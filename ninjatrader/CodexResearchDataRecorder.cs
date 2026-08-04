#region Using declarations
using System;
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
        private const string EventsHeader = "timestamp_utc_ns,instrument,event_type,price,volume,state";
        private const string DepthHeader = "timestamp_utc_ns,instrument,side,operation,position,price,volume,state";

        private readonly object sync = new object();
        private StreamWriter barsWriter;
        private StreamWriter eventsWriter;
        private StreamWriter depthWriter;
        private string outputDirectory;
        private string instrumentKey;
        private string runId;
        private int lastWrittenBar = -1;
        private int barsPart;
        private int eventsPart;
        private int depthPart;
        private long barsRowsSinceFlush;
        private long eventsRowsSinceFlush;
        private long depthRowsSinceFlush;
        private long depthSecond = -1;
        private int depthEventsThisSecond;
        private long depthEventsDropped;
        private bool depthQuotaBlocked;
        private DateTime depthWriterOpenedUtc = DateTime.MinValue;

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
            }
            else if (State == State.DataLoaded)
            {
                OpenFiles();
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
                eventsWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                    "{0},{1},{2},{3:R},{4},{5}", UnixNanos(e.Time), Instrument.FullName,
                    type, e.Price, (long)e.Volume, State));
                eventsRowsSinceFlush++;
                FlushAndRotateIfNeeded(ref eventsWriter, ref eventsRowsSinceFlush,
                    ref eventsPart, "events", EventsHeader);
            }
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
            runId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ", CultureInfo.InvariantCulture);
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
                foreach (StreamWriter writer in new [] { barsWriter, eventsWriter, depthWriter })
                {
                    try { if (writer != null) { writer.Flush(); writer.Close(); } } catch { }
                }
                if (depthEventsDropped > 0)
                    Print(string.Format(CultureInfo.InvariantCulture,
                        "CodexResearchDataRecorder DEPTH_DROPPED_TOTAL instrument={0} dropped={1}",
                        Instrument.FullName, depthEventsDropped));
            }
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
