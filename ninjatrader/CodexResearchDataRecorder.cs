#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

// Contract-aware research recorder for live connections and Playback/Market Replay.
// Historical bars are stored separately from genuine event/depth data. Files rotate
// at a bounded size so a busy contract cannot exhaust the system drive.
namespace NinjaTrader.NinjaScript.Strategies
{
    public class CodexResearchDataRecorder : Strategy
    {
        private const string BarsHeader = "timestamp_utc_ns,instrument,open,high,low,close,volume,state";
        private const int EventSchemaVersion = 2;
        private const int ControlSchemaVersion = 3;
        private const string EventsHeader = "schema_version,recorder_run_id,file_part,record_seq,event_id,timestamp_utc_ns,receive_time_utc_ns,instrument,event_type,price,volume,state";
        private const string ControlHeader = "schema_version,recorder_run_id,control_seq,receive_time_utc_ns,instrument,control_type,status,connection_name,provider,feed_family,details";
        private const string DepthHeader = "timestamp_utc_ns,instrument,side,operation,position,price,volume,state";

        private readonly object sync = new object();
        private StreamWriter barsWriter;
        private StreamWriter eventsWriter;
        private StreamWriter controlWriter;
        private StreamWriter depthWriter;
        private string outputDirectory;
        private string instrumentKey;
        private string runId;
        private int lastWrittenBar = -1;
        private int barsPart;
        private int eventsPart;
        private int depthPart;
        private long eventsRecordSequence;
        private long eventRowsWritten;
        private long eventRowsInPart;
        private long controlSequence;
        private long barsRowsSinceFlush;
        private long eventsRowsSinceFlush;
        private long depthRowsSinceFlush;
        private long depthSecond = -1;
        private int depthEventsThisSecond;
        private long depthEventsDropped;
        private bool depthQuotaBlocked;
        private DateTime depthWriterOpenedUtc = DateTime.MinValue;
        private long writerErrorCount;
        private bool controlWriterFaulted;
        private bool runStopWritten;

        [NinjaScriptProperty]
        public string MarketDataConnectionName { get; set; }

        [NinjaScriptProperty]
        public bool RecordMarketDepth { get; set; }

        [NinjaScriptProperty]
        public bool RecordEventStream { get; set; }

        [NinjaScriptProperty]
        public int MaxFileSizeMB { get; set; }

        [NinjaScriptProperty]
        public int MaxDepthFileMinutes { get; set; }

        [NinjaScriptProperty]
        public int MaxDepthEventsPerSecond { get; set; }

        [NinjaScriptProperty]
        public int MaxDepthDirectoryMB { get; set; }

        [NinjaScriptProperty]
        public int MaxTotalDepthDirectoryMB { get; set; }

        [NinjaScriptProperty]
        public int FlushEveryRows { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "CodexResearchDataRecorder";
                Description = "Exports contract bars and genuine live/replay bid, ask, trade and optional depth events with bounded file rotation.";
                Calculate = Calculate.OnEachTick;
                IsExitOnSessionCloseStrategy = false;
                StartBehavior = StartBehavior.WaitUntilFlat;
                BarsRequiredToTrade = 1;
                IsEnabled = false;
                RecordEventStream = true;
                RecordMarketDepth = false;
                MaxFileSizeMB = 256;
                // Rotate depth files on time as well as size. This bounds the amount of
                // evidence exposed to an interrupted NinjaTrader session without dropping
                // incremental book updates (which would corrupt reconstructed order books).
                MaxDepthFileMinutes = 30;
                // A circuit breaker, not normal sampling. A value of zero disables it.
                // The default is intentionally far above observed event rates.
                MaxDepthEventsPerSecond = 100000;
                // Fail closed instead of deleting unique research evidence. The per-contract
                // cap prevents one very busy book from consuming the whole drive; the global
                // cap leaves bounded headroom across all configured contracts.
                MaxDepthDirectoryMB = 16384;
                MaxTotalDepthDirectoryMB = 120000;
                FlushEveryRows = 5000;
                MarketDataConnectionName = "";
            }
            else if (State == State.DataLoaded)
            {
                OpenFiles();
                NinjaTrader.Cbi.Connection feed = ResolveDataFeedConnection();
                RecordControl("RUN_START", "STARTED", ConnectionName(feed),
                    ProviderName(feed), FeedFamily(feed), RunStartDetails(feed));
                RecordCurrentConnections();
            }
            else if (State == State.Transition || State == State.Realtime)
            {
                RecordControlForConfiguredFeed("STATE_CHANGE",
                    State.ToString().ToUpperInvariant(), "");
            }
            else if (State == State.Terminated)
            {
                CloseFiles();
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || CurrentBar < 1 || !IsFirstTickOfBar)
                return;
            lock (sync)
            {
                int absoluteBar = CurrentBar - 1;
                if (absoluteBar <= lastWrittenBar || barsWriter == null)
                    return;
                DateTime time = Times[0][1];
                barsWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                    "{0},{1},{2:R},{3:R},{4:R},{5:R},{6},{7}",
                    UnixNanos(time), Instrument.FullName, Opens[0][1], Highs[0][1],
                    Lows[0][1], Closes[0][1], (long)Volumes[0][1], State));
                lastWrittenBar = absoluteBar;
                barsRowsSinceFlush++;
                FlushAndRotateIfNeeded(ref barsWriter, ref barsRowsSinceFlush,
                    ref barsPart, "bars", BarsHeader);
            }
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (!RecordEventStream || eventsWriter == null)
                return;
            string type;
            if (e.MarketDataType == MarketDataType.Bid) type = "BID";
            else if (e.MarketDataType == MarketDataType.Ask) type = "ASK";
            else if (e.MarketDataType == MarketDataType.Last) type = "TRADE";
            else return;
            lock (sync)
            {
                try
                {
                    // Identity belongs to the callback, not its payload. Assign it once
                    // before writing so a future retry can reuse the same event ID.
                    long recordSequence = ++eventsRecordSequence;
                    string eventId = string.Format(CultureInfo.InvariantCulture,
                        "{0}:{1:D20}", runId, recordSequence);
                    eventsWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                        "{0},{1},{2},{3},{4},{5},{6},{7},{8},{9:R},{10},{11}",
                        EventSchemaVersion, runId, eventsPart, recordSequence, eventId,
                        UnixNanos(e.Time), UnixNanos(DateTime.UtcNow), Instrument.FullName,
                        type, e.Price, (long)e.Volume, State));
                    eventRowsWritten++;
                    eventRowsInPart++;
                    eventsRowsSinceFlush++;
                    FlushAndRotateIfNeeded(ref eventsWriter, ref eventsRowsSinceFlush,
                        ref eventsPart, "events", EventsHeader);
                }
                catch (Exception ex)
                {
                    WriteControlForConfiguredFeedUnsafe("WRITER_ERROR", "ERROR",
                        "events: " + ex.Message);
                    Print("CodexResearchDataRecorder EVENT_WRITER_ERROR " + ex.Message);
                    try { if (eventsWriter != null) eventsWriter.Close(); } catch { }
                    eventsWriter = null;
                }
            }
        }

        protected override void OnConnectionStatusUpdate(
            ConnectionStatusEventArgs connectionStatusUpdate)
        {
            if (connectionStatusUpdate == null || connectionStatusUpdate.Connection == null)
                return;
            string name = ConnectionName(connectionStatusUpdate.Connection);
            string provider = ProviderName(connectionStatusUpdate.Connection);
            string details = string.Format(CultureInfo.InvariantCulture,
                "order_status={0};price_status={1};error={2};native_error={3}",
                connectionStatusUpdate.Status, connectionStatusUpdate.PriceStatus,
                connectionStatusUpdate.Error, connectionStatusUpdate.NativeError);
            RecordControl("CONNECTION",
                connectionStatusUpdate.PriceStatus.ToString().ToUpperInvariant(),
                name, provider, FeedFamily(connectionStatusUpdate.Connection), details);
        }

        protected override void OnMarketDepth(MarketDepthEventArgs e)
        {
            if (!RecordMarketDepth || depthWriter == null)
                return;
            lock (sync)
            {
                long timestamp = UnixNanos(e.Time);
                long second = timestamp / 1000000000L;
                if (second != depthSecond)
                {
                    depthSecond = second;
                    depthEventsThisSecond = 0;
                }
                if (MaxDepthEventsPerSecond > 0 && depthEventsThisSecond >= MaxDepthEventsPerSecond)
                {
                    depthEventsDropped++;
                    if (depthEventsDropped == 1 || depthEventsDropped % 10000 == 0)
                        Print(string.Format(CultureInfo.InvariantCulture,
                            "CodexResearchDataRecorder DEPTH_THROTTLE instrument={0} dropped={1}",
                            Instrument.FullName, depthEventsDropped));
                    return;
                }
                depthEventsThisSecond++;
                depthWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                    "{0},{1},{2},{3},{4},{5:R},{6},{7}", timestamp, Instrument.FullName,
                    e.MarketDataType, e.Operation, e.Position, e.Price, (long)e.Volume, State));
                depthRowsSinceFlush++;
                FlushAndRotateIfNeeded(ref depthWriter, ref depthRowsSinceFlush,
                    ref depthPart, "depth", DepthHeader);
            }
        }

        private void OpenFiles()
        {
            string docs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            instrumentKey = SafeName(Instrument.FullName);
            outputDirectory = Path.Combine(docs, "trainedData", "autonomous_bot",
                "chatgptIdealNinjaTrader", "cache", "ninjatrader_v2", "raw", instrumentKey);
            Directory.CreateDirectory(outputDirectory);
            // A restart creates a new run identity. The timestamp keeps filenames
            // inspectable while the GUID prevents collisions between rapid restarts.
            runId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffffffZ", CultureInfo.InvariantCulture)
                + "-" + Guid.NewGuid().ToString("N");
            controlWriter = NewWriter("controls", 0, ControlHeader);
            barsWriter = NewWriter("bars", barsPart, BarsHeader);
            if (RecordEventStream)
                eventsWriter = NewWriter("events", eventsPart, EventsHeader);
            if (RecordMarketDepth)
            {
                if (DepthQuotaAllows())
                    depthWriter = NewWriter("depth", depthPart, DepthHeader);
                else
                    LogDepthQuotaBlock();
            }
            Print("CodexResearchDataRecorder -> " + outputDirectory);
        }

        private StreamWriter NewWriter(string kind, int part, string header)
        {
            string suffix = part == 0 ? "" : "_p" + part.ToString("D4", CultureInfo.InvariantCulture);
            string path = Path.Combine(outputDirectory, runId + "_" + kind + suffix + ".csv");
            FileStream stream = new FileStream(path, FileMode.Create, FileAccess.Write,
                FileShare.Read, 1024 * 1024, FileOptions.SequentialScan);
            StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false), 1024 * 1024);
            writer.WriteLine(header);
            writer.Flush();
            if (kind == "depth")
                depthWriterOpenedUtc = DateTime.UtcNow;
            return writer;
        }

        private void FlushAndRotateIfNeeded(ref StreamWriter writer, ref long rows,
                                             ref int part, string kind, string header)
        {
            int flushRows = Math.Max(1, FlushEveryRows);
            bool depthTimeDue = kind == "depth" && MaxDepthFileMinutes > 0 &&
                depthWriterOpenedUtc != DateTime.MinValue &&
                DateTime.UtcNow - depthWriterOpenedUtc >= TimeSpan.FromMinutes(MaxDepthFileMinutes);
            if (rows < flushRows && !depthTimeDue)
                return;
            rows = 0;
            writer.Flush();
            long maxBytes = Math.Max(16, MaxFileSizeMB) * 1024L * 1024L;
            if (!depthTimeDue && writer.BaseStream.Position < maxBytes)
                return;
            writer.Close();
            part++;
            if (kind == "depth" && !DepthQuotaAllows())
            {
                writer = null;
                LogDepthQuotaBlock();
                return;
            }
            writer = NewWriter(kind, part, header);
            if (kind == "events")
                eventRowsInPart = 0;
            Print(string.Format(CultureInfo.InvariantCulture,
                "CodexResearchDataRecorder ROTATE instrument={0} kind={1} part={2} reason={3}",
                Instrument.FullName, kind, part, depthTimeDue ? "duration" : "size"));
        }

        private bool DepthQuotaAllows()
        {
            try
            {
                long contractBytes = DepthBytes(outputDirectory, false);
                long contractLimit = Math.Max(256, MaxDepthDirectoryMB) * 1024L * 1024L;
                if (MaxDepthDirectoryMB > 0 && contractBytes >= contractLimit)
                    return false;

                string rawRoot = Directory.GetParent(outputDirectory).FullName;
                long totalBytes = DepthBytes(rawRoot, true);
                long totalLimit = Math.Max(1024, MaxTotalDepthDirectoryMB) * 1024L * 1024L;
                if (MaxTotalDepthDirectoryMB > 0 && totalBytes >= totalLimit)
                    return false;
                return true;
            }
            catch (Exception ex)
            {
                // Disk-safety checks fail closed. Bars and top-of-book events continue.
                Print("CodexResearchDataRecorder DEPTH_QUOTA_CHECK_ERROR " + ex.Message);
                return false;
            }
        }

        private static long DepthBytes(string root, bool recursive)
        {
            if (string.IsNullOrEmpty(root) || !Directory.Exists(root))
                return 0;
            long total = 0;
            SearchOption option = recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly;
            foreach (string path in Directory.EnumerateFiles(root, "*_depth*.csv", option))
            {
                try { total += new FileInfo(path).Length; } catch { }
            }
            return total;
        }

        private void LogDepthQuotaBlock()
        {
            if (depthQuotaBlocked)
                return;
            depthQuotaBlocked = true;
            Print(string.Format(CultureInfo.InvariantCulture,
                "CodexResearchDataRecorder DEPTH_QUOTA_BLOCK instrument={0} contract_cap_mb={1} total_cap_mb={2}; bars/events continue; archive old depth files before re-enabling depth",
                Instrument.FullName, MaxDepthDirectoryMB, MaxTotalDepthDirectoryMB));
        }

        private void CloseFiles()
        {
            lock (sync)
            {
                if (runStopWritten)
                    return;

                bool clean = writerErrorCount == 0 && !controlWriterFaulted;
                clean = CloseDataWriter(ref barsWriter, "bars") && clean;
                clean = CloseDataWriter(ref eventsWriter, "events") && clean;
                clean = CloseDataWriter(ref depthWriter, "depth") && clean;
                clean = RemoveEmptyTrailingEventPart() && clean;
                if (RecordMarketDepth && (depthEventsDropped > 0 || depthQuotaBlocked))
                    clean = false;

                string details = string.Format(CultureInfo.InvariantCulture,
                    "final_record_seq={0};event_rows={1};final_event_part={2};record_event_stream={3};depth_events_dropped={4};writer_error_count={5}",
                    eventsRecordSequence, eventRowsWritten, eventsPart,
                    RecordEventStream.ToString().ToLowerInvariant(), depthEventsDropped,
                    writerErrorCount);
                WriteControlForConfiguredFeedUnsafe("RUN_STOP",
                    clean ? "CLEAN" : "ERROR", details);
                runStopWritten = true;
                try
                {
                    if (controlWriter != null)
                    {
                        controlWriter.Flush();
                        controlWriter.Close();
                    }
                }
                catch { }
                controlWriter = null;
                if (depthEventsDropped > 0)
                    Print(string.Format(CultureInfo.InvariantCulture,
                        "CodexResearchDataRecorder DEPTH_DROPPED_TOTAL instrument={0} dropped={1}",
                        Instrument.FullName, depthEventsDropped));
            }
        }

        private bool CloseDataWriter(ref StreamWriter writer, string kind)
        {
            if (writer == null)
                return true;
            try
            {
                writer.Flush();
                writer.Close();
                writer = null;
                return true;
            }
            catch (Exception ex)
            {
                writer = null;
                WriteControlForConfiguredFeedUnsafe("WRITER_ERROR", "ERROR",
                    "close_" + kind + ": " + ex.Message);
                return false;
            }
        }

        private bool RemoveEmptyTrailingEventPart()
        {
            if (!RecordEventStream || eventsPart <= 0 || eventRowsInPart != 0)
                return true;
            try
            {
                string path = WriterPath("events", eventsPart);
                if (File.Exists(path) && new FileInfo(path).Length <= EventsHeader.Length + 4)
                {
                    File.Delete(path);
                    eventsPart--;
                }
                return true;
            }
            catch (Exception ex)
            {
                WriteControlForConfiguredFeedUnsafe("WRITER_ERROR", "ERROR",
                    "remove_empty_event_part: " + ex.Message);
                return false;
            }
        }

        private void RecordControl(string controlType, string status,
                                   string connectionName, string provider,
                                   string feedFamily, string details)
        {
            lock (sync)
            {
                WriteControlUnsafe(controlType, status, connectionName, provider,
                    feedFamily, details);
            }
        }

        private void RecordControlForConfiguredFeed(string controlType,
                                                     string status, string details)
        {
            lock (sync)
            {
                WriteControlForConfiguredFeedUnsafe(controlType, status, details);
            }
        }

        private void WriteControlForConfiguredFeedUnsafe(string controlType,
                                                          string status,
                                                          string details)
        {
            NinjaTrader.Cbi.Connection feed = ResolveDataFeedConnection();
            WriteControlUnsafe(controlType, status, ConnectionName(feed),
                ProviderName(feed), FeedFamily(feed), details);
        }

        private void WriteControlUnsafe(string controlType, string status,
                                        string connectionName, string provider,
                                        string feedFamily, string details)
        {
            if (controlWriter == null || string.IsNullOrEmpty(runId))
                return;
            if (controlType == "WRITER_ERROR")
                writerErrorCount++;
            try
            {
                long sequence = ++controlSequence;
                controlWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                    "{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10}", ControlSchemaVersion,
                    Csv(runId), sequence, UnixNanos(DateTime.UtcNow),
                    Csv(Instrument.FullName), Csv(controlType), Csv(status),
                    Csv(connectionName), Csv(provider), Csv(feedFamily), Csv(details)));
                controlWriter.Flush();
            }
            catch (Exception ex)
            {
                controlWriterFaulted = true;
                Print("CodexResearchDataRecorder CONTROL_WRITER_ERROR " + ex.Message);
            }
        }

        private NinjaTrader.Cbi.Connection ResolveDataFeedConnection()
        {
            try
            {
                if (string.IsNullOrWhiteSpace(MarketDataConnectionName))
                    return null;
                lock (NinjaTrader.Cbi.Connection.Connections)
                {
                    foreach (NinjaTrader.Cbi.Connection connection in
                             NinjaTrader.Cbi.Connection.Connections)
                    {
                        if (connection != null && connection.Options != null &&
                            string.Equals(connection.Options.Name,
                                MarketDataConnectionName.Trim(),
                                StringComparison.Ordinal))
                            return connection;
                    }
                }
            }
            catch { }
            return null;
        }

        private string ConnectionName(NinjaTrader.Cbi.Connection connection)
        {
            if (connection != null && connection.Options != null &&
                !string.IsNullOrWhiteSpace(connection.Options.Name))
                return connection.Options.Name;
            return string.IsNullOrWhiteSpace(MarketDataConnectionName)
                ? "UNDECLARED" : MarketDataConnectionName.Trim();
        }

        private static string ProviderName(NinjaTrader.Cbi.Connection connection)
        {
            try
            {
                if (connection != null && connection.Options != null)
                    return connection.Options.Provider.ToString();
            }
            catch { }
            return "UNRESOLVED";
        }

        private static string FeedFamily(NinjaTrader.Cbi.Connection connection)
        {
            return ProviderName(connection);
        }

        private string RunStartDetails(NinjaTrader.Cbi.Connection feed)
        {
            string accountName = Account == null ? "NONE" : Account.Name;
            string accountConnection = "NONE";
            try
            {
                if (Account != null && Account.Connection != null &&
                    Account.Connection.Options != null)
                    accountConnection = Account.Connection.Options.Name;
            }
            catch { }
            string priceStatus = feed == null ? "UNRESOLVED" : feed.PriceStatus.ToString();
            return string.Format(CultureInfo.InvariantCulture,
                "account={0};account_connection={1};price_status={2}",
                accountName, accountConnection, priceStatus);
        }

        private void RecordCurrentConnections()
        {
            try
            {
                List<NinjaTrader.Cbi.Connection> snapshot =
                    new List<NinjaTrader.Cbi.Connection>();
                lock (NinjaTrader.Cbi.Connection.Connections)
                {
                    foreach (NinjaTrader.Cbi.Connection connection in
                             NinjaTrader.Cbi.Connection.Connections)
                        snapshot.Add(connection);
                }
                foreach (NinjaTrader.Cbi.Connection connection in snapshot)
                {
                    RecordControl("CONNECTION",
                        connection.PriceStatus.ToString().ToUpperInvariant(),
                        ConnectionName(connection), ProviderName(connection),
                        FeedFamily(connection), "startup_snapshot");
                }
            }
            catch (Exception ex)
            {
                RecordControl("CONNECTION_SNAPSHOT", "ERROR", "UNRESOLVED",
                    "UNRESOLVED", "UNRESOLVED", ex.Message);
            }
        }

        private string WriterPath(string kind, int part)
        {
            string suffix = part == 0 ? "" : "_p" + part.ToString("D4", CultureInfo.InvariantCulture);
            return Path.Combine(outputDirectory, runId + "_" + kind + suffix + ".csv");
        }

        private static string Csv(string value)
        {
            string text = value ?? "";
            if (text.IndexOfAny(new [] { ',', '"', '\r', '\n' }) < 0)
                return text;
            return "\"" + text.Replace("\"", "\"\"") + "\"";
        }

        private static long UnixNanos(DateTime value)
        {
            return (value.ToUniversalTime().Ticks - 621355968000000000L) * 100L;
        }

        private static string SafeName(string value)
        {
            foreach (char c in Path.GetInvalidFileNameChars()) value = value.Replace(c, '_');
            return value.Replace(' ', '_').Replace('/', '_').Replace('\\', '_');
        }
    }
}
