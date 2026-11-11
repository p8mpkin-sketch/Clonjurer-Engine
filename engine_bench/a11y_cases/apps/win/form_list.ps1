# 自建合成应用(调优集): 列表 + 详情 + 工具条。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f = New-Object System.Windows.Forms.Form
$f.Text = "Tasks - Sample App"; $f.StartPosition = "Manual"; $f.Location = New-Object System.Drawing.Point(20, 20)
$f.Size = New-Object System.Drawing.Size(760, 520); $f.Font = New-Object System.Drawing.Font("Segoe UI", 10)
function Add($c, $x, $y, $w, $h, $t) { $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, $h); $c.Text = $t; $f.Controls.Add($c); $c }
[void](Add (New-Object System.Windows.Forms.Button) 20 16 90 32 "New")
[void](Add (New-Object System.Windows.Forms.Button) 120 16 90 32 "Edit")
[void](Add (New-Object System.Windows.Forms.Button) 220 16 90 32 "Delete")
[void](Add (New-Object System.Windows.Forms.TextBox) 480 18 240 26 "Search tasks")
$l = Add (New-Object System.Windows.Forms.ListBox) 20 64 300 380 ""
"Review pull request", "Write release notes", "Update dependencies", "Plan next sprint", "Fix login redirect", "Archive old reports", "Call the supplier", "Book meeting room" | ForEach-Object { [void]$l.Items.Add($_) }
$l.SelectedIndex = 1
$g = Add (New-Object System.Windows.Forms.GroupBox) 340 64 380 300 "Details"
function AddG($c, $x, $y, $w, $h, $t) { $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, $h); $c.Text = $t; $g.Controls.Add($c); $c }
[void](AddG (New-Object System.Windows.Forms.Label) 16 32 100 24 "Title")
[void](AddG (New-Object System.Windows.Forms.TextBox) 120 30 240 26 "Write release notes")
[void](AddG (New-Object System.Windows.Forms.Label) 16 72 100 24 "Priority")
$cb = AddG (New-Object System.Windows.Forms.ComboBox) 120 70 160 26 ""; "Low", "Normal", "High" | ForEach-Object { [void]$cb.Items.Add($_) }; $cb.SelectedIndex = 2
$d = AddG (New-Object System.Windows.Forms.CheckBox) 120 110 200 24 "Done"
[void](AddG (New-Object System.Windows.Forms.Label) 16 150 100 24 "Progress")
$pb = AddG (New-Object System.Windows.Forms.ProgressBar) 120 150 240 22 ""; $pb.Value = 60
[void](AddG (New-Object System.Windows.Forms.Label) 16 190 344 60 "Notes: collect the merged changes since the last tag and group them by area.")
[void](Add (New-Object System.Windows.Forms.Button) 540 384 180 36 "Save changes")
$s = New-Object System.Windows.Forms.StatusStrip; [void]$s.Items.Add("8 tasks, 1 selected"); $f.Controls.Add($s)
[void]$f.ShowDialog()
