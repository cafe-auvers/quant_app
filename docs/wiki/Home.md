# Quant App Wiki

Quant App is a Windows/PyQt5 desktop dashboard for U.S. swing-trading
research, planning, market-data review, KIS account visibility, and guarded
order execution.

The application is live-trading capable, but production mutation paths are
fail-closed. A visible card, broker acceptance response, or enabled Buy Board
does not prove that an order filled or that execution is safe. Broker truth,
ownership, leases, risk decisions, data freshness, and the shared Live Trading
control remain independent gates.

## Start here

- New operator: [Quick Start](Quick-Start) and [User Workflow](User-Workflow)
- Installation: [Installation and Environment Setup](Installation-and-Environment-Setup)
- Maintainer: [System Overview](System-Overview) and [Architecture](Architecture)
- Live-operation safety: [Risk and Safety Controls](Risk-and-Safety-Controls)
- Incident response: [Operations and Monitoring](Operations-and-Monitoring) and [Troubleshooting](Troubleshooting)

## Status conventions

- **Implemented** means code and automated coverage exist in this repository.
- **Optional** means the feature is implemented but needs local configuration
  or an external service.
- **Mock/test only** means it is not evidence of a real KIS or production run.
- **Disabled by default** means an operator must deliberately satisfy the
  documented gates; never enable it merely to make a warning disappear.
- **Operational validation pending** means code exists but a supervised
  credentialed or physical-machine check is still required.

Canonical repository documentation remains in
[README.md](https://github.com/cafe-auvers/quant_app/blob/master/README.md) and
[PROJECT_ARCHITECTURE.md](https://github.com/cafe-auvers/quant_app/blob/master/PROJECT_ARCHITECTURE.md).
