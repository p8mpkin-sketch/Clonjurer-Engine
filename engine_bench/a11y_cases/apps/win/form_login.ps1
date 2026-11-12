# 自建合成应用(调优集): 登录表单。WinForms 控件自带 UIA 树。
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f = New-Object System.Windows.Forms.Form
$f.Text = "Sign in - Sample App"; $f.StartPosition = "Manual"; $f.Location = New-Object System.Drawing.Point(20, 20)
$f.Size = New-Object System.Drawing.Size(520, 420); $f.Font = New-Object System.Drawing.Font("Segoe UI", 10)
function Add($c, $x, $y, $w, $h, $t) { $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, $h); $c.Text = $t; $f.Controls.Add($c); $c }
$h = Add (New-Object System.Windows.Forms.Label) 30 20 440 36 "Welcome back"; $h.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
[void](Add (New-Object System.Windows.Forms.Label) 30 70 440 24 "Sign in to continue to your workspace.")
[void](Add (New-Object System.Windows.Forms.Label) 30 110 120 24 "Email")
[void](Add (New-Object System.Windows.Forms.TextBox) 160 108 300 26 "alex@example.org")
[void](Add (New-Object System.Windows.Forms.Label) 30 150 120 24 "Password")
$p = Add (New-Object System.Windows.Forms.TextBox) 160 148 300 26 "secret"; $p.UseSystemPasswordChar = $true
$c = Add (New-Object System.Windows.Forms.CheckBox) 160 190 300 24 "Keep me signed in"; $c.Checked = $true
[void](Add (New-Object System.Windows.Forms.RadioButton) 160 220 140 24 "Personal")
$r = Add (New-Object System.Windows.Forms.RadioButton) 310 220 140 24 "Work"; $r.Checked = $true
[void](Add (New-Object System.Windows.Forms.Button) 160 270 140 36 "Sign in")
[void](Add (New-Object System.Windows.Forms.Button) 320 270 140 36 "Cancel")
[void](Add (New-Object System.Windows.Forms.LinkLabel) 160 320 300 24 "Forgot your password?")
[void]$f.ShowDialog()
