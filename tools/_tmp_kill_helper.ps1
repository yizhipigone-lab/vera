# Elevated helper: kill v2rayN and its proxy core processes.
$log = "E:\1target\VERA\_kill_helper_log.txt"
"=== kill helper start $(Get-Date -Format 'HH:mm:ss') ===" | Out-File $log -Encoding utf8
$il = (whoami /groups | Select-String "S-1-16-12288")
"high-integrity: $([bool]$il)" | Out-File $log -Append -Encoding utf8
$names = @("v2rayN", "sing-box", "xray", "v2ray", "hysteria", "naive", "tuic", "juicity")
$killed = @()
foreach ($n in $names) {
  $procs = Get-Process -Name $n -ErrorAction SilentlyContinue
  foreach ($pr in $procs) {
    try {
      Stop-Process -Id $pr.Id -Force -ErrorAction Stop
      $killed += "$($pr.ProcessName) (PID $($pr.Id))"
      "killed: $($pr.ProcessName) PID $($pr.Id)" | Out-File $log -Append -Encoding utf8
    } catch {
      "FAILED: $($pr.ProcessName) PID $($pr.Id): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
  }
}
if (-not $killed) { "nothing matched / nothing killed" | Out-File $log -Append -Encoding utf8 }
Start-Sleep -Seconds 2
$left = Get-Process -Name $names -ErrorAction SilentlyContinue
if ($left) { "still alive: $($left | ForEach-Object { \"$($_.ProcessName):$($_.Id)\" })" -join ', ' | Out-File $log -Append -Encoding utf8 }
else { "RESULT: OK all target processes gone" | Out-File $log -Append -Encoding utf8 }
"=== kill helper end ===" | Out-File $log -Append -Encoding utf8
