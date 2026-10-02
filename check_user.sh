#!/bin/bash
# ============================================================
#  check_user.sh  — MarginWealth support investigation tool
#  Usage:  ./check_user.sh <USER_CODE> [date YYYY-MM-DD]
#  Example: ./check_user.sh CL65758646
#           ./check_user.sh CL65758646 2026-06-18
# ============================================================

USER_CODE="${1:-}"
DATE_ARG="${2:-}"

if [ -z "$USER_CODE" ]; then
  echo "Usage: $0 <USER_CODE> [date YYYY-MM-DD]"
  exit 1
fi

# If date provided, use it; else use today (UTC)
if [ -n "$DATE_ARG" ]; then
  FROM_DATE="${DATE_ARG}T00:00:00Z"
  TO_DATE="${DATE_ARG}T23:59:59Z"
  DATE_LABEL="$DATE_ARG"
else
  FROM_DATE="$(date -u +%Y-%m-%dT00:00:00Z)"
  TO_DATE="$(date -u +%Y-%m-%dT23:59:59Z)"
  DATE_LABEL="$(date -u +%Y-%m-%d) (today)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        MarginWealth — User Investigation Report               ║"
echo "║  User: $USER_CODE   Date: $DATE_LABEL"
echo "╚══════════════════════════════════════════════════════════════╝"

mongosh marginwealth --quiet --eval "
var code = '$USER_CODE';
var from = new Date('$FROM_DATE');
var to   = new Date('$TO_DATE');

// ── 1. USER INFO ─────────────────────────────────────────────
var u = db.users.findOne({user_code: code});
if (!u) { print('❌  User NOT FOUND: ' + code); quit(); }

print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  1. USER INFO');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  Name      : ' + u.full_name);
print('  Code      : ' + u.user_code);
print('  ID        : ' + u._id);
print('  Status    : ' + (u.status || 'N/A'));
print('  Admin ID  : ' + u.assigned_admin_id);
print('  Broker ID : ' + (u.assigned_broker_id || 'N/A'));
print('  Is Demo   : ' + (u.is_demo || false));

// ── 2. WALLET ────────────────────────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  2. WALLET');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var w = db.wallets.findOne({user_id: u._id});
if (w) {
  var avail = w.available_balance       ? w.available_balance['\$numberDecimal']       || w.available_balance       : '0';
  var used  = w.used_margin             ? w.used_margin['\$numberDecimal']             || w.used_margin             : '0';
  var cred  = w.credit_limit            ? w.credit_limit['\$numberDecimal']            || w.credit_limit            : '0';
  var sett  = w.settlement_outstanding  ? w.settlement_outstanding['\$numberDecimal']  || w.settlement_outstanding  : '0';
  var rpnl  = w.realized_pnl            ? w.realized_pnl['\$numberDecimal']            || w.realized_pnl            : '0';
  var upnl  = w.unrealized_pnl          ? w.unrealized_pnl['\$numberDecimal']          || w.unrealized_pnl          : '0';
  var bal   = (parseFloat(avail) + parseFloat(used) + parseFloat(cred)).toFixed(2);
  print('  Balance (avail+margin+credit): ₹' + bal);
  print('  Available Balance   : ₹' + avail);
  print('  Used Margin         : ₹' + used);
  print('  Credit Limit        : ₹' + cred);
  print('  Realized PnL        : ₹' + rpnl);
  print('  Unrealized PnL      : ₹' + upnl);
  print('  Settlement Outstanding: ₹' + sett + (parseFloat(sett) > 0 ? '  ⚠️  SHORTFALL' : ''));
} else {
  print('  ⚠️  No wallet found');
}

// ── 3. RISK SETTINGS ─────────────────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  3. RISK SETTINGS (resolved hierarchy)');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var ur = db.user_risk_settings.findOne({user_id: u._id});
var ar = u.assigned_admin_id  ? db.sub_admin_risk_settings.findOne({sub_admin_id: u.assigned_admin_id}) : null;
var br = u.assigned_broker_id ? db.broker_risk_settings.findOne({broker_id: u.assigned_broker_id})     : null;
var sr = db.super_admin_risk_settings.findOne({});
function riskVal(field) {
  if (ur && ur[field] != null) return ur[field] + ' (user)';
  if (br && br[field] != null) return br[field] + ' (broker)';
  if (ar && ar[field] != null) return ar[field] + ' (admin)';
  if (sr && sr[field] != null) return sr[field] + ' (platform)';
  return 'not set';
}
print('  Stop-Out %            : ' + riskVal('stopOutPercent'));
print('  Stop-Out Warning %    : ' + riskVal('stopOutWarningPercent'));
print('  Profit Hold (sec)     : ' + riskVal('profitTradeHoldMinSeconds'));
print('  Loss Hold (sec)       : ' + riskVal('lossTradeHoldMinSeconds'));

// ── 4. OPEN POSITIONS ────────────────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  4. OPEN POSITIONS (' + from.toISOString().slice(0,10) + ')');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var openPos = db.positions.find({user_id: u._id, status: 'OPEN'}).toArray();
if (openPos.length === 0) {
  print('  No open positions');
} else {
  openPos.forEach(function(p) {
    var avg = p.avg_price ? p.avg_price['\$numberDecimal'] || p.avg_price : '?';
    var upnl = p.unrealized_pnl ? p.unrealized_pnl['\$numberDecimal'] || p.unrealized_pnl : '0';
    var margin = p.margin_used ? p.margin_used['\$numberDecimal'] || p.margin_used : '0';
    var pnlSign = parseFloat(upnl) >= 0 ? '+' : '';
    print('  ' + p.instrument.symbol + ' | ' + (p.quantity > 0 ? 'BUY' : 'SELL') + ' ' + Math.abs(p.quantity) + ' | avg ₹' + avg + ' | M2M ' + pnlSign + '₹' + upnl + ' | margin ₹' + margin + ' | opened ' + (p.opened_at ? p.opened_at.toISOString().replace('T',' ').slice(0,19) + ' UTC' : '?'));
  });
}

// ── 5. TODAY'S CLOSED POSITIONS ──────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  5. CLOSED POSITIONS (date range)');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var closedPos = db.positions.find({user_id: u._id, status: 'CLOSED', closed_at: {\$gte: from, \$lte: to}}).sort({closed_at: -1}).toArray();
if (closedPos.length === 0) {
  print('  No closed positions in this date range');
} else {
  closedPos.forEach(function(p) {
    var avg  = p.avg_price     ? p.avg_price['\$numberDecimal']     || p.avg_price     : '?';
    var rpnl = p.realized_pnl  ? p.realized_pnl['\$numberDecimal']  || p.realized_pnl  : '0';
    var pnlSign = parseFloat(rpnl) >= 0 ? '+' : '';
    var reason = p.close_reason || 'USER';
    var reasonFlag = (reason === 'STOP_OUT') ? ' ⚠️ STOP-OUT' : (reason === 'SL_HIT') ? ' 🔴 SL HIT' : (reason === 'TP_HIT') ? ' 🟢 TP HIT' : '';
    print('  ' + p.instrument.symbol + ' | avg ₹' + avg + ' | PnL ' + pnlSign + '₹' + rpnl + ' | ' + reason + reasonFlag + ' | closed ' + (p.closed_at ? p.closed_at.toISOString().replace('T',' ').slice(0,19) + ' UTC' : '?'));
  });
}

// ── 6. TODAY'S ORDERS ────────────────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  6. ORDERS (date range) — last 20');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var orders = db.orders.find({user_id: u._id, created_at: {\$gte: from, \$lte: to}}).sort({created_at: -1}).limit(20).toArray();
if (orders.length === 0) {
  print('  No orders in this date range');
} else {
  orders.forEach(function(o) {
    var statusFlag = o.status === 'REJECTED' ? ' ❌ REJECTED: ' + (o.rejection_reason || '') : (o.status === 'EXECUTED' ? ' ✅' : ' ⏳');
    var sq = o.is_squareoff ? ' [SQUAREOFF]' : '';
    print('  ' + o.instrument.symbol + ' | ' + o.action + ' ' + o.quantity + sq + ' | ' + o.status + statusFlag + ' | ' + o.created_at.toISOString().replace('T',' ').slice(0,19) + ' UTC');
  });
}

// ── 7. WALLET TRANSACTIONS TODAY ─────────────────────────────
print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  7. WALLET TRANSACTIONS (date range)');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
var wtxns = db.wallet_transactions.find({user_id: u._id, created_at: {\$gte: from, \$lte: to}}).sort({created_at: 1}).toArray();
if (wtxns.length === 0) {
  print('  No wallet transactions in this date range');
} else {
  wtxns.forEach(function(t) {
    var amt  = t.amount       ? t.amount['\$numberDecimal']       || t.amount       : '0';
    var bal  = t.balance_after ? t.balance_after['\$numberDecimal'] || t.balance_after : '?';
    var sign = parseFloat(amt) >= 0 ? '+' : '';
    var settFlag = t.transaction_type === 'SETTLEMENT_OUTSTANDING_BOOKED' ? ' ⚠️ SHORTFALL' : '';
    print('  ' + t.created_at.toISOString().replace('T',' ').slice(0,19) + ' | ' + t.transaction_type + ' | ' + sign + '₹' + amt + ' | bal ₹' + bal + settFlag);
  });
}

print('');
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('  ✅  Investigation complete for ' + code);
print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
print('');
"
