"""Windows Task Scheduler job for hourly comment-to-DM runs (local-only app, no server).

The task runs scripts/engage-run.cmd while the user is logged in. If the PC was off/asleep, one missed run starts
as soon as it's back (StartWhenAvailable). Overlap with a manual run is prevented by the engine's lock file.
"""

import subprocess
from pathlib import Path

from app.domain.errors import DomainError

TASK_NAME = "CommentToDM-EngageRun"
BOARD_TASK_NAME = "CommentToDM-ControlBoard"


def _powershell(script: str) -> str:
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise DomainError(f"Task Scheduler: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def install(runner: Path, every_minutes: int) -> dict:
    if not 15 <= every_minutes <= 24 * 60:
        raise DomainError("every must be between 15 and 1440 minutes")
    script = f"""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -Command "& ''' + {_ps_quote(str(runner))}.Replace("'", "''") + '''"')
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes({every_minutes}) -RepetitionInterval (New-TimeSpan -Minutes {every_minutes})
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Comment to DM: reply + DM to keyword comments (engage-run --auto)' -Force | Out-Null
"""
    _powershell(script)
    return status()


def remove() -> dict:
    _powershell(f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue")
    return status()


def status(task: str = TASK_NAME) -> dict:
    out = _powershell(
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(task)} -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
        "$fmt = { param($d) if ($d) { $d.ToString('s') } else { '' } }; "
        "$rep = $t.Triggers | ForEach-Object { $_.Repetition.Interval } | Where-Object { $_ } | Select-Object -First 1; "
        "$every = if ($rep) { [int][System.Xml.XmlConvert]::ToTimeSpan($rep).TotalMinutes } else { '' }; "
        "'{0}|{1}|{2}|{3}|{4}' -f $t.State, (& $fmt $i.LastRunTime), (& $fmt $i.NextRunTime), $i.LastTaskResult, $every } else { 'none' }"
    )
    if out == "none" or not out:
        return {"installed": False, "task": task}
    state, last, nxt, result, every = (out.split("|") + [""] * 5)[:5]
    out_status = {"installed": True, "task": task, "state": state, "last_run": last, "next_run": nxt, "last_result": result}
    if every.strip().isdigit():
        out_status["every_minutes"] = int(every)
    return out_status


def install_board_autostart(script: Path) -> dict:
    """Start the control board (hidden) and open it in the browser at every Windows logon."""
    ps = f"""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + {_ps_quote(str(script))} + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName {_ps_quote(BOARD_TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Comment to DM: start the local control board at logon' -Force | Out-Null
"""
    _powershell(ps)
    return status(BOARD_TASK_NAME)


def remove_board_autostart() -> dict:
    _powershell(f"Unregister-ScheduledTask -TaskName {_ps_quote(BOARD_TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue")
    return status(BOARD_TASK_NAME)


class WindowsTaskScheduler:
    """TaskScheduler port over the functions above."""

    def __init__(self, runner: Path):
        self.runner = runner

    def install(self, every_minutes: int) -> dict:
        return install(self.runner, every_minutes)

    def remove(self) -> dict:
        return remove()

    def status(self) -> dict:
        return status()
