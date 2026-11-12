# 自建合成应用(调优集): 编辑器式界面 —— 菜单栏、纯图标工具条、标签页、带立体边框的多行编辑区(含滚动条)、自绘面板、状态栏。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
function Icon($color, $size) {
  $b = New-Object System.Drawing.Bitmap($size, $size)
  $g = [System.Drawing.Graphics]::FromImage($b)
  $g.Clear([System.Drawing.Color]::Transparent)
  $br = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromName($color))
  $g.FillRectangle($br, 2, 3, $size - 4, $size - 6)
  $g.DrawLine([System.Drawing.Pens]::Black, 3, $size - 4, $size - 3, 4)
  $g.Dispose(); $b
}
$f = New-Object System.Windows.Forms.Form
$f.Text = "draft-notes.txt - Plain Pad (Sample App)"; $f.StartPosition = "Manual"; $f.Location = New-Object System.Drawing.Point(20, 20)
$f.Size = New-Object System.Drawing.Size(820, 560)
$tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = "Fill"
"draft-notes.txt", "outline.md" | ForEach-Object { $tp = New-Object System.Windows.Forms.TabPage($_); [void]$tabs.TabPages.Add($tp) }
$split = New-Object System.Windows.Forms.SplitContainer; $split.Dock = "Fill"; $split.SplitterDistance = 420
$ed = New-Object System.Windows.Forms.RichTextBox; $ed.Dock = "Fill"; $ed.BorderStyle = "Fixed3D"; $ed.ScrollBars = "ForcedBoth"; $ed.WordWrap = $false
$ed.Font = New-Object System.Drawing.Font("Consolas", 10)
$ed.Text = "# Draft notes`nship the capture workflow`ncheck the scores on the new samples`n`n- item one: gather inputs`n- item two: compare outputs`n- item three: write it up`n`ntotal = 3 items"
$split.Panel1.Controls.Add($ed)
$pn = New-Object System.Windows.Forms.Panel; $pn.Dock = "Fill"; $pn.BorderStyle = "Fixed3D"; $pn.BackColor = [System.Drawing.Color]::White
$pn.Add_Paint({ param($s, $e)
  $g = $e.Graphics
  $vals = 40, 90, 60, 130, 75, 110
  for ($i = 0; $i -lt $vals.Count; $i++) {
    $br = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(40 + 30 * $i, 110, 200 - 20 * $i))
    $g.FillRectangle($br, 30 + $i * 50, 180 - $vals[$i], 34, $vals[$i]) }
  $g.DrawLine([System.Drawing.Pens]::Black, 20, 180, 340, 180); $g.DrawLine([System.Drawing.Pens]::Black, 20, 20, 20, 180) })
$split.Panel2.Controls.Add($pn)
$tabs.TabPages[0].Controls.Add($split)
$f.Controls.Add($tabs)
$ts = New-Object System.Windows.Forms.ToolStrip
"New:White", "Open:Khaki", "Save:SteelBlue", "Cut:Crimson", "Copy:LightGray", "Paste:Tan", "Undo:Orange", "Redo:Orange", "Find:SeaGreen" | ForEach-Object {
  $p = $_.Split(":"); $b = New-Object System.Windows.Forms.ToolStripButton($p[0], (Icon $p[1] 16)); $b.DisplayStyle = "Image"; [void]$ts.Items.Add($b)
  if ($p[0] -in "Save", "Paste", "Redo") { [void]$ts.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) } }
$f.Controls.Add($ts)
$ms = New-Object System.Windows.Forms.MenuStrip
"File", "Edit", "Search", "View", "Format", "Language", "Window", "Help" | ForEach-Object { [void]$ms.Items.Add($_) }
$f.MainMenuStrip = $ms; $f.Controls.Add($ms)
$ss = New-Object System.Windows.Forms.StatusStrip
"Plain text", "length: 172   lines: 9", "Ln 1, Col 1", "Windows (CR LF)", "UTF-8" | ForEach-Object { [void]$ss.Items.Add($_) }
$f.Controls.Add($ss)
[void]$f.ShowDialog()
