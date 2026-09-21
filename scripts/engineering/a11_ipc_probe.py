"""Record existing IPC outcomes; do not mutate arguments, result, state or budgets."""
import json
import os
from pathlib import Path
from time import monotonic
EVENTS=[]


def pytest_sessionstart(session):
    from ostag.briefs.runtime import BriefStatusOwner, _Fault
    original=BriefStatusOwner._exchange
    async def observed(self,*args,**kwargs):
        start=monotonic()
        event=dict(boot_id=self.boot_id,request=args[0],phase=args[1],outcome='ok')
        try:
            result=await original(self,*args,**kwargs)
            if isinstance(result,dict):
                event.update(result_reason=result.get('reason'),control_generation=result.get('control_generation'))
            return result
        except BaseException as exc:
            event.update(outcome=type(exc).__name__,fault=str(exc) if isinstance(exc,_Fault) else None)
            raise
        finally:
            event.update(seconds=monotonic()-start,status_reason=self.reason,status_health=self.health)
            EVENTS.append(event)
    BriefStatusOwner._exchange=observed


def pytest_sessionfinish(session,exitstatus):
    p=Path(os.environ['OSTAG_IPC_PROBE_OUTPUT'])
    p.write_text(json.dumps(dict(exitstatus=int(exitstatus),events=EVENTS),indent=2)+'\n')
    failures=[e for e in EVENTS if e['outcome']!='ok']
    summary=dict(exitstatus=int(exitstatus),calls=len(EVENTS),max_seconds=max((e['seconds'] for e in EVENTS),default=0),failures=failures)
    # Fault injection intentionally fails IPC in several tests. These observations
    # are diagnostics, not additional acceptance predicates or reasons to retry.
    (p.parent/'mac-ipc-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
