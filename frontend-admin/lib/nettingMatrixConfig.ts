// Mirror of bharat_indian_funded's nettingMatrixConfig.js — drives the netting
// segment matrix UI. Kept in sync manually with the backend NettingFieldsBase.

export type FieldType = "number" | "select" | "time";

export interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  options?: { v: string | boolean; l: string }[];
  optionOnly?: boolean;
  notForOption?: boolean;
  futureOnly?: boolean;
  // Escape hatch for a field that lives in a gated category but applies to
  // EVERY segment. "Cancel pending orders at" sits under Expiry day, yet
  // forex — which has no expiry at all — is the main reason it exists.
  allSegments?: boolean;
}

export interface CategoryDef {
  id: string;
  label: string;
}

export interface SegmentRow {
  code: string;
  name: string;
  lotApplies: boolean;
  qtyApplies: boolean;
  optionApplies: boolean;
  expiryHoldApplies: boolean;
  futureApplies: boolean;
}

export const SETTING_CATEGORIES: CategoryDef[] = [
  { id: "lot", label: "Lot" },
  { id: "quantity", label: "Quantity" },
  { id: "value", label: "Value" },
  { id: "fixedMargin", label: "Fixed Margin" },
  { id: "options", label: "Options" },
  { id: "brokerage", label: "Brokerage" },
  { id: "limitPoint", label: "Limit away" },
  { id: "spread", label: "Spread" },
  { id: "block", label: "Block" },
  { id: "expiryHold", label: "Expiry day" },
];

export const CATEGORY_FIELDS: Record<string, FieldDef[]> = {
  lot: [
    { key: "minLots", label: "Min Lot", type: "number" },
    { key: "orderLots", label: "Per Order Lot", type: "number" },
    { key: "maxLots", label: "Max Lot/Script", type: "number" },
    { key: "maxExchangeLots", label: "Max Exchange Lots", type: "number" },
  ],
  quantity: [
    { key: "minQty", label: "Min Qty", type: "number" },
    { key: "perOrderQty", label: "Per Order Qty", type: "number" },
    { key: "maxQtyPerScript", label: "Max Qty/Script", type: "number" },
  ],
  value: [{ key: "maxValue", label: "Max margin value (₹)", type: "number" }],
  fixedMargin: [
    {
      key: "marginCalcMode",
      label: "Margin Mode",
      type: "select",
      // Fixed = flat ₹ per lot (the field value is rupees, charged
      // once per lot regardless of price).
      // Times = leverage multiplier (e.g. 100 → 100× leverage → margin
      // is notional ÷ 100).
      // Percent has been retired — old docs still resolve via the
      // legacy fallback in netting_service so nothing breaks.
      options: [
        { v: "fixed", l: "Fixed" },
        { v: "times", l: "Times" },
      ],
    },
    { key: "intradayMargin", label: "Intraday Margin", type: "number" },
    { key: "overnightMargin", label: "Overnight Margin", type: "number" },
    {
      key: "optionBuyMarginCalcMode",
      label: "Opt Buy Mode",
      // Empty value = inherit segment-level marginCalcMode (the common
      // case). The Cell component sends "" up to saveAll, which the
      // backend upsert accepts and writes as null on the override doc.
      type: "select",
      optionOnly: true,
      options: [
        { v: "", l: "Inherit" },
        { v: "fixed", l: "Fixed" },
        { v: "times", l: "Times" },
      ],
    },
    { key: "optionBuyIntraday", label: "Opt Buy Intraday", type: "number", optionOnly: true },
    { key: "optionBuyOvernight", label: "Opt Buy Overnight", type: "number", optionOnly: true },
    {
      key: "optionSellMarginCalcMode",
      label: "Opt Sell Mode",
      type: "select",
      optionOnly: true,
      options: [
        { v: "", l: "Inherit" },
        { v: "fixed", l: "Fixed" },
        { v: "times", l: "Times" },
      ],
    },
    { key: "optionSellIntraday", label: "Opt Sell Intraday", type: "number", optionOnly: true },
    { key: "optionSellOvernight", label: "Opt Sell Overnight", type: "number", optionOnly: true },
  ],
  options: [
    // Single % cap that drives both the order-validator strike-far check
    // AND the option-chain dialog's strike filter. The chain only shows
    // strikes within ±X% of the underlying spot. Hidden on non-OPT rows
    // via `optionOnly`.
    { key: "strikeFarPercent", label: "Max % from underlying", type: "number", optionOnly: true },
  ],
  brokerage: [
    {
      key: "commissionType",
      label: "Type",
      type: "select",
      options: [
        { v: "per_lot", l: "Per Lot" },
        { v: "per_crore", l: "Per Crore" },
      ],
    },
    { key: "commission", label: "Commission (₹)", type: "number", notForOption: true },
    { key: "optionBuyCommission", label: "Buy Brokerage (₹)", type: "number", optionOnly: true },
    { key: "optionSellCommission", label: "Sell Brokerage (₹)", type: "number", optionOnly: true },
    {
      key: "chargeOn",
      label: "Charge On",
      type: "select",
      options: [
        { v: "open", l: "Open" },
        { v: "close", l: "Close" },
        { v: "both", l: "Both" },
      ],
    },
  ],
  limitPoint: [
    {
      // When Yes, this segment refuses parked/pending orders (LIMIT / SL /
      // SL-M) — only market entries + exits go through. Default No.
      key: "blockPendingOrders",
      label: "Pending orders blocked",
      type: "select",
      options: [
        { v: false, l: "No" },
        { v: true, l: "Yes" },
      ],
    },
    {
      // Per-segment Stop-Loss toggle. Yes = users may attach an SL and the
      // risk enforcer auto-fires it. No = SL blocked for this segment (order
      // validator + edit-SL/TP endpoint reject it, enforcer won't auto-fire
      // an existing leg). Independent of the Target toggle below.
      key: "slEnabled",
      label: "Stop Loss enabled",
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
    {
      // Per-segment Take-Profit (Target) toggle — same semantics as SL.
      key: "tpEnabled",
      label: "Target (TP) enabled",
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
    {
      // Block a parked LIMIT/SL order from resting INSIDE the day's traded
      // range (between session high & low). Independent of "Max % away".
      // Default No — nothing changes until an admin turns it on.
      key: "limitWithinDayRange",
      label: "Block limit inside day High/Low",
      type: "select",
      options: [
        { v: false, l: "No" },
        { v: true, l: "Yes" },
      ],
    },
  ],
  spread: [
    {
      key: "spreadType",
      label: "Spread Type",
      type: "select",
      options: [
        { v: "fixed", l: "Fixed" },
        { v: "floating", l: "Floating" },
      ],
    },
    { key: "spreadPips", label: "Spread (pips)", type: "number" },
    {
      key: "swapType",
      label: "Swap Type",
      type: "select",
      options: [
        { v: "points", l: "Points" },
        { v: "percentage", l: "Percentage" },
      ],
    },
    { key: "swapLong", label: "Swap Long", type: "number" },
    { key: "swapShort", label: "Swap Short", type: "number" },
    { key: "swapTime", label: "Swap Time (IST)", type: "time" },
  ],
  block: [
    {
      key: "isActive",
      label: "Is Active",
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
    {
      key: "tradingEnabled",
      label: "Trading Enabled",
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
    {
      key: "allowOvernight",
      label: "Allow Overnight",
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
  ],
  expiryHold: [
    { key: "expiryProfitHoldMinSeconds", label: "Expiry profit hold (s)", type: "number" },
    { key: "expiryLossHoldMinSeconds", label: "Expiry loss hold (s)", type: "number" },
    {
      key: "pendingOrderExpiryTime",
      label: "Cancel pending orders at (IST)",
      allSegments: true,
      // Blank = never: this segment's unfilled LIMIT / SL-M orders carry.
      // Set a time and they are cancelled once a day at it, and positions
      // carrying overnight also lose their SL / TP.
      type: "time",
    },
    {
      key: "expiryNoNewTradeDays",
      label: "No new trades in last N days",
      // Counts calendar days and includes expiry day itself: 3 on a Thursday
      // expiry blocks Tue, Wed and Thu. Closing stays allowed throughout.
      type: "number",
    },
    {
      key: "expiryDayMarginAsPercent",
      label: "Expiry margin as %",
      // When Yes, the three expiry-day margin numbers below are percent
      // of notional. When No, they're flat ₹ per lot — same shape as the
      // segment's Fixed margin mode. Lets admin run normal trading on
      // Times-leverage but switch to flat ₹ on expiry to discourage
      // last-minute carries.
      type: "select",
      options: [
        { v: true, l: "Yes" },
        { v: false, l: "No" },
      ],
    },
    { key: "expiryDayIntradayMargin", label: "Expiry day margin (futures)", type: "number", futureOnly: true },
    { key: "expiryDayOptionBuyMargin", label: "Expiry day OPT BUY margin", type: "number", optionOnly: true },
    { key: "expiryDayOptionSellMargin", label: "Expiry day OPT SELL margin", type: "number", optionOnly: true },
  ],
};

// Admin matrix rows whose underlying instruments don't settle daily —
// there is no concept of an overnight margin for these segments. Keep
// in sync with INTRADAY_ONLY_ADMIN_ROWS in
// backend/app/services/netting_service.py.
const INTRADAY_ONLY_ROWS = new Set(["FOREX", "STOCKS", "INDICES", "COMMODITIES", "CRYPTO"]);

// Field keys that represent an overnight / carryforward dimension of margin.
// Hidden for the Infoway segments above so admins don't enter values that
// would never be picked up by the resolver.
const OVERNIGHT_FIELD_KEYS = new Set([
  "overnightMargin",
  "optionBuyOvernight",
  "optionSellOvernight",
  "expiryDayIntradayMargin",
  "expiryDayOptionBuyMargin",
  "expiryDayOptionSellMargin",
  "allowOvernight",
]);

export function isFieldNA(segment: SegmentRow | undefined, categoryId: string, field: FieldDef): boolean {
  if (!segment) return true;
  if (field.allSegments) return false;
  if (field.optionOnly && !segment.optionApplies) return true;
  if (field.notForOption && segment.optionApplies) return true;
  if (field.futureOnly && !segment.futureApplies) return true;
  if (categoryId === "lot" && !segment.lotApplies) return true;
  if (categoryId === "quantity" && !segment.qtyApplies) return true;
  if (categoryId === "options" && !segment.optionApplies) return true;
  if (categoryId === "expiryHold" && !segment.expiryHoldApplies) return true;
  if (INTRADAY_ONLY_ROWS.has(segment.code) && OVERNIGHT_FIELD_KEYS.has(field.key)) return true;
  return false;
}
