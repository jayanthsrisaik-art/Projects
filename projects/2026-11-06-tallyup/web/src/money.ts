// Client-side mirror of tallyup/money.py parse/format rules. The server is the
// source of truth; this exists so the form can reject bad input before a round
// trip. Both implementations are tested against samples/money_vectors.json.

export class MoneyError extends Error {}

const AMOUNT_RE = /^(-?)(\d{1,12})(?:\.(\d+))?$/;
export const MAX_MINOR = 1e15; // well inside Number.MAX_SAFE_INTEGER

export interface ParseOptions {
  allowNegative?: boolean;
  allowZero?: boolean;
}

export function parseAmount(text: string, exponent: number, opts: ParseOptions = {}): number {
  const m = AMOUNT_RE.exec(text.trim());
  if (!m) throw new MoneyError(`invalid amount "${text}"`);
  const [, sign, whole] = m;
  let frac = m[3] ?? "";
  if (frac.length > exponent) {
    if (/[^0]/.test(frac.slice(exponent))) {
      throw new MoneyError(`amount "${text}" has more than ${exponent} decimal places`);
    }
    frac = frac.slice(0, exponent);
  }
  let value = Number(whole) * 10 ** exponent + (exponent ? Number(frac.padEnd(exponent, "0")) : 0);
  if (sign) value = -value;
  if (value < 0 && !opts.allowNegative) throw new MoneyError("amount must not be negative");
  if (value === 0 && !opts.allowZero) throw new MoneyError("amount must be greater than zero");
  if (Math.abs(value) > MAX_MINOR) throw new MoneyError("amount too large");
  return value === 0 ? 0 : value; // normalise -0
}

export function formatAmount(minor: number, exponent: number): string {
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  if (exponent === 0) return `${sign}${abs}`;
  const unit = 10 ** exponent;
  const whole = Math.floor(abs / unit);
  const frac = String(abs % unit).padStart(exponent, "0");
  return `${sign}${whole}.${frac}`;
}
