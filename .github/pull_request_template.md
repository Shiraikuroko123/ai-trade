## Summary

-

## Research And Risk Impact

- Timing/look-ahead impact:
- Turnover/cost impact:
- Paper/live-trading impact:
- Market-data/provenance impact:
- Release and documentation impact:

## Validation

- [ ] `python -m compileall -q src tests`
- [ ] `python -m unittest discover -s tests -v`
- [ ] `ruff check .` and `node --check src/ai_trade/web/assets/app.js`
- [ ] Full-history result reviewed
- [ ] Walk-forward result reviewed
- [ ] Desktop/mobile workstation behavior reviewed when UI code changes
- [ ] Wheel and source distribution pass `scripts/verify_distribution.py` when packaging changes
- [ ] Documentation and changelog updated
- [ ] No credentials, caches, account state, reports, or logs included
