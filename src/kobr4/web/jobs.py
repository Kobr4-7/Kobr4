"""Calculs longs lancés depuis le site (backtests, optimisations), hors de la boucle du
serveur pour ne jamais ralentir les bots."""

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from typing import Any

from kobr4.backtest.engine import load_bars, run_backtest
from kobr4.backtest.metrics import compute_metrics
from kobr4.config.settings import Settings
from kobr4.lab.optimizer import LabSettings, Optimizer
from kobr4.marketdata.sources.synthetic import MARKER
from kobr4.marketdata.store import ParquetBarStore

log = logging.getLogger(__name__)


def backtest_job(
    settings_json: str, store: str, start: datetime | None, end: datetime | None
) -> dict[str, Any]:
    """Exécuté dans un processus séparé."""
    settings = Settings.model_validate_json(settings_json)
    st = ParquetBarStore(store)
    bars = load_bars(st, settings, start, end)
    missing = [s for s, b in bars.items() if not b]
    if missing:
        raise ValueError(
            f"historique absent pour {', '.join(missing)} : lancer `kobr4 data download`"
        )
    result = asyncio.run(run_backtest(settings, bars))
    m = compute_metrics(result.initial_balance, result.equity_curve, result.trades)
    curve = result.equity_curve
    step = max(1, len(curve) // 600)
    points = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
    return {
        "metrics": {
            k: (None if isinstance(v, float) and v != v else v) for k, v in m.as_dict().items()
        },
        "equity": [{"ts": ts.isoformat(), "equity": float(eq)} for ts, eq in points],
        "trades": [
            {
                "symbol": t.symbol,
                "side": str(t.side),
                "quantity": t.quantity,
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price),
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
                "reason": str(t.reason),
                "pnl": float(t.pnl),
            }
            for t in result.trades[-300:]
        ],
        "rejections": result.rejections,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "initial_balance": float(result.initial_balance),
        "final_equity": float(result.final_equity),
        "synthetic": (st.root / MARKER).exists(),
    }


def optimize_job(
    settings_json: str, strategy_id: str, store: str, lab_json: str, start: datetime, end: datetime
) -> dict[str, Any]:
    settings = Settings.model_validate_json(settings_json)
    lab = LabSettings.model_validate_json(lab_json)
    proposal = Optimizer(settings, strategy_id, store, lab).run(start, end)
    return proposal.model_dump(mode="json")


class JobRunner:
    def __init__(
        self, backtest_executor: Executor | None = None, lab_executor: Executor | None = None
    ) -> None:
        self.backtests = backtest_executor or ProcessPoolExecutor(max_workers=2)
        self.lab = lab_executor or ThreadPoolExecutor(max_workers=1)
        self.tasks: set[asyncio.Task[None]] = set()

    def submit(
        self,
        executor: Executor,
        fn: Callable[..., Any],
        *args: Any,
        done: Callable[[Any, str | None], Any],
    ) -> None:
        async def run() -> None:
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(executor, fn, *args)
            except Exception as e:
                log.warning("tâche %s en échec : %s", getattr(fn, "__name__", fn), e)
                await done(None, str(e))
                return
            await done(result, None)

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def shutdown(self) -> None:
        self.backtests.shutdown(wait=False, cancel_futures=True)
        self.lab.shutdown(wait=False, cancel_futures=True)
