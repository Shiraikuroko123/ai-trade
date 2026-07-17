# AI Trade QMT Read-Only Adapter

This optional Windows plugin connects AI Trade to a user-owned, already logged-in
QMT/miniQMT client through the broker-supplied `xtquant` package. It reads account
assets, positions, cancelable orders, and today's fills.

The adapter is intentionally read-only:

- it rejects live mode;
- `submit_orders` and `cancel_order` always fail before calling QMT;
- QMT does not expose a reliable paper/live discriminator, so observations are
  operator-configured and cannot count as qualifying sandbox reconciliation;
- the package does not bundle, download, or redistribute `xtquant`, QMT DLLs, or
  broker credentials.

Install it from a source checkout after installing AI Trade:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps -e .\adapters\qmt
```

Configuration and probe commands are documented in
`docs/BROKER_ADAPTERS.md` in the repository root.
