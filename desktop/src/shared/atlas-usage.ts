/** Never invent a balance: capacities come from the active server ledger. */
export function atlasUsageMeter(data: Record<string, unknown>) {
  const number = (value: unknown) => Math.max(0, Number(value) || 0);
  const monthly = number(data.monthly_balance_tokens);
  const prepaid = monthly <= 0;
  const remaining = prepaid ? number(data.payg_balance_tokens) : monthly;
  const plan = data.plan as Record<string, unknown> | undefined;
  const capacity = Math.max(remaining, number(prepaid ? data.payg_capacity_tokens : data.monthly_capacity_tokens) || (!prepaid ? number(plan?.monthly_tokens) : 0));
  const percent = capacity ? Math.min(100, Math.round(100 * remaining / capacity)) : 0;
  return { remaining, capacity, percent, prepaid };
}
