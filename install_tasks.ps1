# Создаёт задачи Планировщика для FocusLock по расписанию из config.json.
# Запускать заново после любых изменений времени: старые задачи FocusLock удаляются и создаются заново.
$cfg = Get-Content "$PSScriptRoot\config.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$py = (Get-Command pythonw.exe -ErrorAction Stop).Source  # pythonw из PATH

Get-ScheduledTask -TaskName "FocusLock *" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false

foreach ($p in $cfg.slots.PSObject.Properties) {
    $name = "FocusLock $($p.Name)"
    $triggers = @()
    $desc = @()
    foreach ($s in $p.Value.schedule) {
        if ($s.days -contains "daily") {
            $triggers += New-ScheduledTaskTrigger -Daily -At $s.time
            $desc += "каждый день $($s.time)"
        } else {
            $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $s.days -At $s.time
            $desc += "$($s.days -join ', ') $($s.time)"
        }
    }
    $a = New-ScheduledTaskAction -Execute $py -Argument "-X utf8 `"$PSScriptRoot\focus.py`" run $($p.Name)" -WorkingDirectory $PSScriptRoot
    $st = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $a -Trigger $triggers -Settings $st -Description $p.Value.title | Out-Null
    "$name ($($p.Value.minutes) мин, $($p.Value.title)): $($desc -join '; ')"
}
