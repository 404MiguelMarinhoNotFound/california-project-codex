# Builds callaudio.dex: the phone-side call-audio helper (see src/CallAudio.java).
# Needs a JDK (javac) and Google's r8.jar (d8). The android.* stubs are compiled
# only to satisfy javac and are deliberately left out of the dex.
param(
  [string]$R8 = "$env:USERPROFILE\tools\r8\r8.jar"
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$build = Join-Path $here 'build'
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force "$build\stubs", "$build\classes", "$build\dex" | Out-Null

$javac = (Get-Command javac -ErrorAction SilentlyContinue).Source
if (-not $javac) {
  $javac = (Get-ChildItem "$env:USERPROFILE\tools\jdk-*\bin\javac.exe", 'C:\Program Files\Microsoft\jdk-*\bin\javac.exe' -ErrorAction SilentlyContinue |
    Select-Object -Last 1).FullName
}
if (-not $javac) { throw 'No javac found: install a JDK (winget install Microsoft.OpenJDK.21)' }
$java = Join-Path (Split-Path $javac) 'java.exe'

& $javac --release 11 -d "$build\stubs" (Get-ChildItem "$here\stubs" -Recurse -Filter *.java).FullName
& $javac --release 11 -cp "$build\stubs" -d "$build\classes" "$here\src\CallAudio.java"
if ($LASTEXITCODE) { throw "javac failed" }
$classes = (Get-ChildItem "$build\classes" -Filter *.class).FullName
& $java -cp $R8 com.android.tools.r8.D8 --min-api 33 --output "$build\dex" $classes
if ($LASTEXITCODE) { throw "d8 failed" }
Copy-Item "$build\dex\classes.dex" "$here\callaudio.dex" -Force
"built $here\callaudio.dex"
