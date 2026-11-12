# 自建合成应用(调优集): 文件管理器式界面 —— 菜单栏、图标在上文字在下的工具条、地址栏、带图标的多列列表、状态栏。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
function Icon($color, $size) {
  $b = New-Object System.Drawing.Bitmap($size, $size)
  $g = [System.Drawing.Graphics]::FromImage($b)
  $g.Clear([System.Drawing.Color]::Transparent)
  $br = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromName($color))
  $g.FillEllipse($br, 2, 2, $size - 4, $size - 4)
  $g.DrawRectangle([System.Drawing.Pens]::Black, 5, 5, $size - 11, $size - 11)
  $g.Dispose(); $b
}
$f = New-Object System.Windows.Forms.Form
$f.Text = "Shelf Browser - Sample App"; $f.StartPosition = "Manual"; $f.Location = New-Object System.Drawing.Point(20, 20)
$f.Size = New-Object System.Drawing.Size(820, 560)
$list = New-Object System.Windows.Forms.ListView
$list.View = "Details"; $list.Dock = "Fill"; $list.FullRowSelect = $true
$il = New-Object System.Windows.Forms.ImageList; $il.ImageSize = New-Object System.Drawing.Size(16, 16)
$il.Images.Add((Icon "Goldenrod" 16)); $il.Images.Add((Icon "SteelBlue" 16)); $list.SmallImageList = $il
"Name:220", "Size:80", "Changed:150", "Kind:120", "Owner:120" | ForEach-Object { $p = $_.Split(":"); [void]$list.Columns.Add($p[0], [int]$p[1]) }
$rows = @(@("Blueprints", "", "2026-03-02 09:14", "Folder", "dana"), @("Invoices", "", "2026-03-05 16:40", "Folder", "lee"),
          @("Photos", "", "2026-02-21 11:02", "Folder", "dana"), @("budget.xlsx", "48 210", "2026-03-06 08:55", "Spreadsheet", "kim"),
          @("notes.txt", "1 204", "2026-03-06 10:31", "Text file", "dana"), @("roadmap.pdf", "912 004", "2026-01-30 14:22", "Document", "lee"),
          @("todo.md", "640", "2026-03-07 07:48", "Text file", "kim"))
foreach ($r in $rows) { $it = New-Object System.Windows.Forms.ListViewItem($r[0], $(if ($r[1] -eq "") { 0 } else { 1 })); 1..4 | ForEach-Object { [void]$it.SubItems.Add($r[$_]) }; [void]$list.Items.Add($it) }
$list.Items[4].Selected = $true
$f.Controls.Add($list)
$addr = New-Object System.Windows.Forms.ComboBox; $addr.Dock = "Top"; $addr.Text = "C:\Shelves\Projects\Current"; $f.Controls.Add($addr)
$ts = New-Object System.Windows.Forms.ToolStrip; $ts.ImageScalingSize = New-Object System.Drawing.Size(24, 24)
"New:ForestGreen", "Open:RoyalBlue", "Rename:DarkOrange", "Move:Purple", "Delete:Crimson", "Info:Gold" | ForEach-Object {
  $p = $_.Split(":"); $b = New-Object System.Windows.Forms.ToolStripButton($p[0], (Icon $p[1] 24)); $b.TextImageRelation = "ImageAboveText"; [void]$ts.Items.Add($b) }
$f.Controls.Add($ts)
$ms = New-Object System.Windows.Forms.MenuStrip
"File", "Edit", "View", "Bookmarks", "Tools", "Help" | ForEach-Object { [void]$ms.Items.Add($_) }
$f.MainMenuStrip = $ms; $f.Controls.Add($ms)
$ss = New-Object System.Windows.Forms.StatusStrip; [void]$ss.Items.Add("7 items"); [void]$ss.Items.Add("1 selected, 1 204 bytes"); $f.Controls.Add($ss)
[void]$f.ShowDialog()
