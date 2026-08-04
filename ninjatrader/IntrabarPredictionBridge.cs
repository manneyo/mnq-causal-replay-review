#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class IntrabarPredictionBridge : Strategy
    {
        private const string BridgeVersion = "IPB-1.2";
        private TcpListener listener;
        private Thread serverThread;
        private volatile bool running;
        private readonly object commandLock = new object();
        private readonly Queue<string> commandQueue = new Queue<string>();
        private readonly object eventLock = new object();
        private readonly List<string> eventLog = new List<string>();
        private readonly HashSet<string> acceptedClientIds = new HashSet<string>();
        private readonly Dictionary<string, string> signalToClient = new Dictionary<string, string>();
        private readonly Dictionary<string, string> clientSide = new Dictionary<string, string>();
        private readonly Dictionary<string, int> clientQty = new Dictionary<string, int>();
        private readonly Dictionary<string, double> clientEntryPrice = new Dictionary<string, double>();
        private readonly Dictionary<string, int> clientEntryFilledQty = new Dictionary<string, int>();
        private readonly Dictionary<string, string> clientState = new Dictionary<string, string>();
        private readonly object snapshotLock = new object();
        private bool hasCompletedBar;
        private DateTime completedBarTime;
        private double completedOpen;
        private double completedHigh;
        private double completedLow;
        private double completedClose;
        private long completedVolume;
        private long eventSequence;
        private int listenPort = 5556;
        private int maxEntriesPerDirection = 10;
        private int maxContracts = 1;
        private int maxQueueDepth = 100;
        private bool tradingEnabled;
        private bool drawSignals = true;
        private bool enableLogging = true;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "IntrabarPredictionBridge";
                Description = "Idempotent intrabar prediction bridge with per-entry brackets and chart markers.";
                Calculate = Calculate.OnEachTick;
                EntriesPerDirection = 10;
                EntryHandling = EntryHandling.UniqueEntries;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                BarsRequiredToTrade = 1;
                tradingEnabled = false;
            }
            else if (State == State.Configure)
            {
                EntriesPerDirection = Math.Max(1, maxEntriesPerDirection);
            }
            else if (State == State.DataLoaded)
            {
                StartServer();
            }
            else if (State == State.Terminated)
            {
                StopServer();
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < 1) return;
            if (IsFirstTickOfBar)
            {
                lock (snapshotLock)
                {
                    completedBarTime = Time[1];
                    completedOpen = Open[1];
                    completedHigh = High[1];
                    completedLow = Low[1];
                    completedClose = Close[1];
                    completedVolume = Convert.ToInt64(Volume[1]);
                    hasCompletedBar = true;
                }
            }
            string command = null;
            lock (commandLock)
            {
                if (commandQueue.Count > 0)
                    command = commandQueue.Dequeue();
            }
            if (!string.IsNullOrWhiteSpace(command))
                ExecuteCommand(command);
        }

        protected override void OnOrderUpdate(
            Order order,
            double limitPrice,
            double stopPrice,
            int quantity,
            int filled,
            double averageFillPrice,
            OrderState orderState,
            DateTime time,
            ErrorCode error,
            string nativeError)
        {
            if (order == null) return;
            string clientId = ResolveClientId(order);
            if (string.IsNullOrEmpty(clientId)) return;
            if (orderState == OrderState.Accepted || orderState == OrderState.Working)
            {
                lock (eventLock)
                {
                    string state;
                    if (!clientState.TryGetValue(clientId, out state)
                        || (state != "OPEN" && state != "CLOSED"))
                        clientState[clientId] = "ACCEPTED";
                }
                AppendEvent("ACCEPTED", clientId, time, quantity, averageFillPrice, orderState.ToString());
            }
            else if (orderState == OrderState.Rejected || orderState == OrderState.Cancelled)
            {
                lock (eventLock)
                {
                    string state;
                    if (!clientState.TryGetValue(clientId, out state) || state != "CLOSED")
                        clientState[clientId] = "REJECTED";
                }
                AppendEvent("REJECTED", clientId, time, quantity, averageFillPrice,
                    error + ":" + (nativeError ?? ""));
            }
        }

        protected override void OnExecutionUpdate(
            Execution execution,
            string executionId,
            double price,
            int quantity,
            MarketPosition marketPosition,
            string orderId,
            DateTime time)
        {
            if (execution == null || execution.Order == null) return;
            string clientId = ResolveClientId(execution.Order);
            if (string.IsNullOrEmpty(clientId)) return;
            bool isExit = !string.IsNullOrEmpty(execution.Order.FromEntrySignal);
            double realizedPnl = 0.0;
            lock (eventLock)
            {
                if (!isExit)
                {
                    int previousQty = clientEntryFilledQty.ContainsKey(clientId)
                        ? clientEntryFilledQty[clientId] : 0;
                    double previousPrice = clientEntryPrice.ContainsKey(clientId)
                        ? clientEntryPrice[clientId] : 0.0;
                    int totalQty = previousQty + quantity;
                    clientEntryPrice[clientId] = totalQty > 0
                        ? (previousPrice * previousQty + price * quantity) / totalQty
                        : price;
                    clientEntryFilledQty[clientId] = totalQty;
                    clientState[clientId] = "OPEN";
                }
                else if (clientEntryPrice.ContainsKey(clientId))
                {
                    int sign = clientSide.ContainsKey(clientId) && clientSide[clientId] == "SELL" ? -1 : 1;
                    realizedPnl = (price - clientEntryPrice[clientId]) * sign * quantity
                        * Instrument.MasterInstrument.PointValue;
                    int remaining = clientEntryFilledQty.ContainsKey(clientId)
                        ? Math.Max(0, clientEntryFilledQty[clientId] - quantity) : 0;
                    clientEntryFilledQty[clientId] = remaining;
                    clientState[clientId] = remaining > 0 ? "OPEN" : "CLOSED";
                }
            }
            AppendEvent(isExit ? "CLOSED" : "FILLED", clientId, time, quantity, price,
                execution.Order.Name ?? "execution", realizedPnl);
            if (drawSignals && isExit)
            {
                Draw.Dot(this, "exit_" + clientId + "_" + eventSequence, false, 0, price, Brushes.Gold);
            }
        }

        private string ResolveClientId(Order order)
        {
            string signal = !string.IsNullOrEmpty(order.FromEntrySignal)
                ? order.FromEntrySignal
                : order.Name;
            if (string.IsNullOrEmpty(signal)) return null;
            lock (eventLock)
            {
                string clientId;
                return signalToClient.TryGetValue(signal, out clientId) ? clientId : null;
            }
        }

        private void AppendEvent(string type, string clientId, DateTime time, int quantity,
            double price, string reason, double realizedPnl = 0.0)
        {
            string side = "BUY";
            lock (eventLock)
            {
                if (clientSide.ContainsKey(clientId)) side = clientSide[clientId];
                long sequence = ++eventSequence;
                double epoch = (time.ToUniversalTime().Ticks - 621355968000000000L)
                    / (double)TimeSpan.TicksPerSecond;
                string safeReason = (reason ?? "").Replace("|", "/").Replace(";", ",");
                eventLog.Add(string.Format(CultureInfo.InvariantCulture,
                    "{0}|{1}|{2}|{3:F3}|{4}|{5}|{6:F8}|{7}|{8:F8}",
                    sequence, type, clientId, epoch, side, quantity, price, safeReason,
                    realizedPnl));
                if (eventLog.Count > 2000) eventLog.RemoveAt(0);
            }
        }

        private int CountWorkingAccountOrders()
        {
            if (Account == null || Instrument == null) return 0;
            int count = 0;
            foreach (Order order in Account.Orders)
            {
                if (order == null || order.Instrument == null
                    || order.Instrument.FullName != Instrument.FullName)
                    continue;
                string state = order.OrderState.ToString();
                if (state != "Filled" && state != "Cancelled"
                    && state != "Rejected" && state != "Unknown")
                    count++;
            }
            return count;
        }

        private void StartServer()
        {
            try
            {
                listener = new TcpListener(IPAddress.Loopback, listenPort);
                listener.Start();
                running = true;
                serverThread = new Thread(ServerLoop) { IsBackground = true };
                serverThread.Start();
                Print(Name + " " + BridgeVersion + " listening on " + listenPort
                    + (tradingEnabled ? " [ARMED]" : " [DISARMED]"));
            }
            catch (Exception exception)
            {
                Print("Bridge start failed: " + exception.Message);
            }
        }

        private void StopServer()
        {
            running = false;
            try { if (listener != null) listener.Stop(); } catch { }
            try { if (serverThread != null) serverThread.Join(2000); } catch { }
        }

        private void ServerLoop()
        {
            while (running)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    Thread worker = new Thread(() => HandleClient(client)) { IsBackground = true };
                    worker.Start();
                }
                catch
                {
                    if (!running) break;
                }
            }
        }

        private void HandleClient(TcpClient client)
        {
            try
            {
                using (client)
                using (NetworkStream stream = client.GetStream())
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
                {
                    writer.AutoFlush = true;
                    string line = reader.ReadLine();
                    if (string.IsNullOrWhiteSpace(line)) return;
                    string response = ProcessRequest(line.Trim());
                    writer.WriteLine(response);
                }
            }
            catch (Exception exception)
            {
                if (enableLogging) Print("Bridge client error: " + exception.Message);
            }
        }

        private string ProcessRequest(string raw)
        {
            string[] parts = raw.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) return "ERR EMPTY";
            string action = parts[0].ToUpperInvariant();
            if (action == "PING") return "OK";
            if (action == "VERSION")
                return "VERSION " + BridgeVersion + " " + (tradingEnabled ? "ARMED" : "DISARMED");
            if (action == "SAFETY")
            {
                bool simOnly = Account != null && !string.IsNullOrEmpty(Account.Name)
                    && Account.Name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase);
                return "SAFETY " + (simOnly ? "SIM_ONLY" : "NON_SIM") + " "
                    + (tradingEnabled ? "ARMED" : "DISARMED");
            }
            if (action == "ACCOUNT")
                return Account == null || string.IsNullOrEmpty(Account.Name)
                    ? "ACCOUNT NONE" : "ACCOUNT " + Account.Name;
            if (action == "STATUS")
                return "STATUS " + PositionAccount.MarketPosition + " " + PositionAccount.Quantity;
            if (action == "ORDERS")
                return "ORDERS " + CountWorkingAccountOrders();
            if (action == "INSTRUMENT")
                return Instrument == null || Instrument.MasterInstrument == null
                    ? "INSTRUMENT NONE"
                    : "INSTRUMENT " + Instrument.MasterInstrument.Name;
            if (action == "SNAPSHOT")
            {
                lock (snapshotLock)
                {
                    if (!hasCompletedBar) return "SNAPSHOT NONE";
                    double epoch = (completedBarTime.ToUniversalTime().Ticks - 621355968000000000L)
                        / (double)TimeSpan.TicksPerSecond;
                    return string.Format(CultureInfo.InvariantCulture,
                        "SNAPSHOT {0:F3} {1:F8} {2:F8} {3:F8} {4:F8} {5}",
                        epoch, completedOpen, completedHigh, completedLow, completedClose,
                        completedVolume);
                }
            }
            if (action == "EVENTS")
            {
                long cursor = 0;
                if (parts.Length > 1) long.TryParse(parts[1], out cursor);
                List<string> pending = new List<string>();
                lock (eventLock)
                {
                    foreach (string item in eventLog)
                    {
                        string[] fields = item.Split('|');
                        long sequence;
                        if (fields.Length > 0 && long.TryParse(fields[0], out sequence) && sequence > cursor)
                            pending.Add(item);
                    }
                }
                return pending.Count == 0 ? "EVENTS NONE" : "EVENTS " + string.Join(";", pending);
            }
            if (action == "EVENTRANGE")
            {
                lock (eventLock)
                {
                    long oldest = 0;
                    if (eventLog.Count > 0)
                    {
                        string[] fields = eventLog[0].Split('|');
                        long.TryParse(fields[0], out oldest);
                    }
                    return "EVENTRANGE " + oldest + " " + eventSequence;
                }
            }
            if (action == "CLIENT")
            {
                if (parts.Length != 2) return "ERR CLIENT_FORMAT";
                string lookupId = Sanitize(parts[1]);
                if (string.IsNullOrEmpty(lookupId)) return "ERR CLIENT_ID";
                lock (eventLock)
                {
                    string state;
                    return "CLIENT " + lookupId + " "
                        + (clientState.TryGetValue(lookupId, out state) ? state : "UNKNOWN");
                }
            }
            if (Account == null || string.IsNullOrEmpty(Account.Name)
                || !Account.Name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase))
                return "ERR SIM_ONLY";
            if (action == "EXIT")
            {
                if (parts.Length != 2) return "ERR EXIT_FORMAT";
                string clientId = Sanitize(parts[1]);
                if (string.IsNullOrEmpty(clientId)) return "ERR CLIENT_ID";
                lock (commandLock)
                {
                    if (commandQueue.Count >= maxQueueDepth) return "ERR QUEUE_FULL";
                    commandQueue.Enqueue("EXIT " + clientId);
                }
                return "ACK " + clientId + " EXIT_QUEUED";
            }
            if (action == "FLAT")
            {
                lock (commandLock)
                {
                    if (commandQueue.Count >= maxQueueDepth) return "ERR QUEUE_FULL";
                    commandQueue.Enqueue("FLAT ALL");
                }
                return "ACK FLAT QUEUED";
            }
            if (!tradingEnabled) return "ERR DISARMED";
            if (action == "ORDER")
            {
                if (parts.Length != 6) return "ERR ORDER_FORMAT";
                string clientId = Sanitize(parts[1]);
                if (string.IsNullOrEmpty(clientId)) return "ERR CLIENT_ID";
                int requestedQuantity;
                if (!int.TryParse(parts[3], out requestedQuantity) || requestedQuantity <= 0)
                    return "ERR QUANTITY";
                if (requestedQuantity > Math.Max(1, maxContracts))
                    return "ERR MAX_CONTRACTS";
                if (PositionAccount.MarketPosition != MarketPosition.Flat || PositionAccount.Quantity != 0)
                    return "ERR POSITION_NOT_FLAT";
                if (CountWorkingAccountOrders() != 0)
                    return "ERR WORKING_ORDERS";
                lock (commandLock)
                {
                    if (acceptedClientIds.Contains(clientId))
                        return "ACK " + clientId + " DUPLICATE";
                    lock (eventLock)
                    {
                        foreach (string state in clientState.Values)
                        {
                            if (state == "QUEUED" || state == "ACCEPTED" || state == "OPEN")
                                return "ERR ACTIVE_ORDER";
                        }
                    }
                    if (commandQueue.Count >= maxQueueDepth) return "ERR QUEUE_FULL";
                    acceptedClientIds.Add(clientId);
                    lock (eventLock) clientState[clientId] = "QUEUED";
                    commandQueue.Enqueue(string.Join(" ", parts));
                }
                return "ACK " + clientId + " QUEUED";
            }
            return "ERR UNKNOWN_COMMAND";
        }

        private void ExecuteCommand(string raw)
        {
            try
            {
                string[] parts = raw.Split(' ');
                string action = parts[0].ToUpperInvariant();
                if (action == "EXIT" && parts.Length == 2)
                {
                    string exitClientId = Sanitize(parts[1]);
                    string entrySignal = null;
                    string entrySide = "BUY";
                    int remaining = 0;
                    lock (eventLock)
                    {
                        foreach (KeyValuePair<string, string> item in signalToClient)
                        {
                            if (item.Value == exitClientId)
                            {
                                entrySignal = item.Key;
                                break;
                            }
                        }
                        if (clientSide.ContainsKey(exitClientId))
                            entrySide = clientSide[exitClientId];
                        if (clientEntryFilledQty.ContainsKey(exitClientId))
                            remaining = clientEntryFilledQty[exitClientId];
                    }
                    if (string.IsNullOrEmpty(entrySignal) || remaining <= 0)
                    {
                        AppendEvent("REJECTED", exitClientId, Time[0], 0, Close[0],
                            "exit requested without an open tracked entry");
                        return;
                    }
                    string exitSignal = "IPB_Exit_" + exitClientId;
                    if (entrySide == "SELL") ExitShort(exitSignal, entrySignal);
                    else ExitLong(exitSignal, entrySignal);
                    return;
                }
                if (action == "FLAT")
                {
                    Account.Flatten(new[] { Instrument });
                    return;
                }
                if (action != "ORDER" || parts.Length != 6) return;
                string clientId = Sanitize(parts[1]);
                if (!tradingEnabled || Account == null || string.IsNullOrEmpty(Account.Name)
                    || !Account.Name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase))
                {
                    lock (eventLock) clientState[clientId] = "REJECTED";
                    AppendEvent("REJECTED", clientId, Time[0], 0, Close[0],
                        "entry execution blocked by safety state");
                    return;
                }
                string side = parts[2].ToUpperInvariant();
                int quantity;
                double stopPrice;
                double targetPrice;
                if (!int.TryParse(parts[3], out quantity)
                    || !double.TryParse(parts[4], NumberStyles.Float, CultureInfo.InvariantCulture, out stopPrice)
                    || !double.TryParse(parts[5], NumberStyles.Float, CultureInfo.InvariantCulture, out targetPrice)
                    || quantity <= 0 || quantity > Math.Max(1, maxContracts)
                    || stopPrice <= 0 || targetPrice <= 0)
                {
                    lock (eventLock) clientState[clientId] = "REJECTED";
                    AppendEvent("REJECTED", clientId, Time[0], Math.Max(quantity, 0), Close[0], "invalid order values");
                    return;
                }
                bool validBracket = side == "BUY"
                    ? stopPrice < Close[0] && targetPrice > Close[0]
                    : stopPrice > Close[0] && targetPrice < Close[0];
                if ((side != "BUY" && side != "SELL") || !validBracket)
                {
                    lock (eventLock) clientState[clientId] = "REJECTED";
                    AppendEvent("REJECTED", clientId, Time[0], quantity, Close[0], "invalid bracket orientation");
                    return;
                }

                string signal = "IPB_" + clientId;
                lock (eventLock)
                {
                    signalToClient[signal] = clientId;
                    clientSide[clientId] = side;
                    clientQty[clientId] = quantity;
                }
                SetStopLoss(signal, CalculationMode.Price, stopPrice, false);
                SetProfitTarget(signal, CalculationMode.Price, targetPrice);
                if (side == "BUY") EnterLong(quantity, signal);
                else EnterShort(quantity, signal);

                if (drawSignals)
                {
                    Brush entryBrush = side == "BUY" ? Brushes.DodgerBlue : Brushes.Red;
                    if (side == "BUY")
                        Draw.ArrowUp(this, "entry_" + clientId, false, 0, Low[0] - 2 * TickSize, entryBrush);
                    else
                        Draw.ArrowDown(this, "entry_" + clientId, false, 0, High[0] + 2 * TickSize, entryBrush);
                    Draw.HorizontalLine(this, "stop_" + clientId, false, stopPrice,
                        Brushes.Crimson, NinjaTrader.Gui.DashStyleHelper.Dash, 1);
                    Draw.HorizontalLine(this, "target_" + clientId, false, targetPrice,
                        Brushes.LimeGreen, NinjaTrader.Gui.DashStyleHelper.Dash, 1);
                }
                AppendEvent("QUEUED", clientId, Time[0], quantity, Close[0], "submitted to NinjaTrader");
            }
            catch (Exception exception)
            {
                if (enableLogging) Print("Execute command failed: " + exception.Message);
            }
        }

        private static string Sanitize(string value)
        {
            StringBuilder builder = new StringBuilder();
            foreach (char character in value ?? "")
            {
                if (char.IsLetterOrDigit(character) || character == '_' || character == '-')
                    builder.Append(character);
                if (builder.Length >= 48) break;
            }
            return builder.ToString();
        }

        [NinjaScriptProperty]
        public int ListenPort
        {
            get { return listenPort; }
            set { listenPort = value; }
        }

        [NinjaScriptProperty]
        public int MaxEntriesPerDirection
        {
            get { return maxEntriesPerDirection; }
            set { maxEntriesPerDirection = value; }
        }

        [NinjaScriptProperty]
        public int MaxContracts
        {
            get { return maxContracts; }
            set { maxContracts = value; }
        }

        [NinjaScriptProperty]
        public bool TradingEnabled
        {
            get { return tradingEnabled; }
            set { tradingEnabled = value; }
        }

        [NinjaScriptProperty]
        public bool DrawSignals
        {
            get { return drawSignals; }
            set { drawSignals = value; }
        }

        [NinjaScriptProperty]
        public bool EnableLogging
        {
            get { return enableLogging; }
            set { enableLogging = value; }
        }
    }
}
