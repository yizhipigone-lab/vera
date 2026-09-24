# Elevated helper: click the TUN toggle in the v2rayN window.
# Log: E:\1target\VERA\_tun_helper_log.txt  (ASCII-only on purpose: powershell 5.1
# reads BOM-less UTF-8 as GBK and Chinese chars break the parser)
$log = "E:\1target\VERA\_tun_helper_log.txt"
"=== helper start $(Get-Date -Format 'HH:mm:ss') ===" | Out-File $log -Encoding utf8

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$il = (whoami /groups | Select-String "S-1-16-12288")
"high-integrity: $([bool]$il)" | Out-File $log -Append -Encoding utf8

$p = Get-Process -Name v2rayN -ErrorAction SilentlyContinue
if (-not $p) { "v2rayN not running" | Out-File $log -Append -Encoding utf8; exit 1 }
$h = $p.MainWindowHandle
"window handle: $h  title: $($p.MainWindowTitle)" | Out-File $log -Append -Encoding utf8

$root = [System.Windows.Automation.AutomationElement]::FromHandle($h)
if ($null -eq $root) { "no UIA root" | Out-File $log -Append -Encoding utf8; exit 2 }

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$script:tunEl = $null
$script:dump = New-Object System.Collections.Generic.List[string]
function Walk($el, $depth) {
  if ($depth -gt 10 -or $script:dump.Count -gt 400) { return }
  $c = $el.Current
  $ct = $c.ControlType.ProgrammaticName
  $nm = $c.Name
  $script:dump.Add(("{0}[{1}] '{2}' AutoId='{3}' Class='{4}'" -f (' ' * ($depth * 2)), $ct, $nm, $c.AutomationId, $c.ClassName))
  if ($null -eq $script:tunEl -and $nm -match '^(Tun|TUN)$') {
    $script:tunEl = $el
  }
  $child = $walker.GetFirstChild($el)
  while ($null -ne $child) {
    Walk $child ($depth + 1)
    $child = $walker.GetNextSibling($child)
  }
}
try { Walk $root 0 } catch { "walk error: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
"tree nodes: $($script:dump.Count)" | Out-File $log -Append -Encoding utf8
$script:dump | Out-File $log -Append -Encoding utf8

if ($null -eq $script:tunEl) {
  "no exact Tun/TUN name; fuzzy fallback (name contains Tun)" | Out-File $log -Append -Encoding utf8
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "Tun")
  $f = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  "name=='Tun' hits: $($f.Count)" | Out-File $log -Append -Encoding utf8
  foreach ($e in $f) { "  hit: [$($e.Current.ControlType.ProgrammaticName)] '$($e.Current.Name)'" | Out-File $log -Append -Encoding utf8 }
  if ($f.Count -gt 0) { $script:tunEl = $f[0] }
}

if ($null -eq $script:tunEl) {
  "RESULT: FAIL no TUN element" | Out-File $log -Append -Encoding utf8
  exit 3
}

"found TUN element: [$($script:tunEl.Current.ControlType.ProgrammaticName)] '$($script:tunEl.Current.Name)'" | Out-File $log -Append -Encoding utf8

$toggled = $false
try {
  $tp = $script:tunEl.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
  $before = $tp.Current.ToggleState
  $tp.Toggle()
  Start-Sleep -Milliseconds 500
  $after = $tp.Current.ToggleState
  "TogglePattern: $before -> $after" | Out-File $log -Append -Encoding utf8
  $toggled = $true
  "RESULT: OK toggled $before -> $after" | Out-File $log -Append -Encoding utf8
} catch {
  "no TogglePattern: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}
if (-not $toggled) {
  try {
    $ip = $script:tunEl.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $ip.Invoke()
    "InvokePattern: invoked" | Out-File $log -Append -Encoding utf8
    "RESULT: OK invoked" | Out-File $log -Append -Encoding utf8
  } catch {
    "no InvokePattern: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    "RESULT: FAIL no usable pattern" | Out-File $log -Append -Encoding utf8
    exit 4
  }
}
"=== helper end ===" | Out-File $log -Append -Encoding utf8
