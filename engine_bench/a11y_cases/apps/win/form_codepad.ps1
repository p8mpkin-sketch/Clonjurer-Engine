# 自建合成应用(调优集): 代码编辑器式界面 —— 自绘编辑区(行号栏 + 等宽文本, 全文只在可访问名里)作为独立子窗口叠在
# 标签页控件上面(同级, Z 序在前), 标签页控件外套一圈深色线框。复现"同级窗口互相叠放 + 自绘内容"这类结构。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f = New-Object System.Windows.Forms.Form
$f.Text = "todo.cfg - Code Pad (Sample App)"; $f.StartPosition = "Manual"; $f.Location = New-Object System.Drawing.Point(20, 20)
$f.Size = New-Object System.Drawing.Size(820, 560)
$ms = New-Object System.Windows.Forms.MenuStrip
"File", "Edit", "Search", "View", "Encoding", "Settings", "Run", "Help" | ForEach-Object { [void]$ms.Items.Add($_) }
$ss = New-Object System.Windows.Forms.StatusStrip
"Config file", "length: 148   lines: 8", "Ln 1, Col 1", "Unix (LF)", "UTF-8" | ForEach-Object { [void]$ss.Items.Add($_) }
$cw = $f.ClientSize.Width; $ch = $f.ClientSize.Height
$frame = New-Object System.Windows.Forms.Panel; $frame.BackColor = [System.Drawing.Color]::DimGray
$frame.Location = New-Object System.Drawing.Point(0, 26); $frame.Size = New-Object System.Drawing.Size($cw, ($ch - 26 - 24))
$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Location = New-Object System.Drawing.Point(1, 1); $tabs.Size = New-Object System.Drawing.Size(($cw - 2), ($ch - 26 - 24 - 2))
"todo.cfg", "notes.ini" | ForEach-Object { [void]$tabs.TabPages.Add((New-Object System.Windows.Forms.TabPage($_))) }
$frame.Controls.Add($tabs)
$lines = "name = sample", "mode = fast", "retries = 3", "timeout = 45   # seconds", "", "[paths]", "cache = ./var/cache", "logs = ./var/logs"
$ed = New-Object System.Windows.Forms.Panel; $ed.BackColor = [System.Drawing.Color]::White
$ed.Location = New-Object System.Drawing.Point(4, 52); $ed.Size = New-Object System.Drawing.Size(($cw - 8), ($ch - 52 - 24 - 5))
$ed.AccessibleName = ($lines -join "`r`n")
$ed.Add_Paint({ param($s, $e)
  $g = $e.Graphics
  $mono = New-Object System.Drawing.Font("Consolas", 10)
  $gut = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(228, 228, 228))
  $g.FillRectangle($gut, 0, 0, 46, $s.Height)
  $hl = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(232, 232, 255))
  $g.FillRectangle($hl, 46, 2, $s.Width - 46, 16)
  for ($i = 0; $i -lt $lines.Count; $i++) {
    $g.DrawString(("{0,3}" -f ($i + 1)), $mono, [System.Drawing.Brushes]::Gray, 8, 2 + $i * 16)
    $g.DrawString($lines[$i], $mono, [System.Drawing.Brushes]::Black, 52, 2 + $i * 16) } })
$f.Controls.Add($ed); $f.Controls.Add($frame)
$f.MainMenuStrip = $ms; $f.Controls.Add($ms); $f.Controls.Add($ss)
$ed.BringToFront()
[void]$f.ShowDialog()
