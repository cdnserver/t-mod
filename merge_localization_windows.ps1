param(
    [string]$ExamplePath,
    [string]$TargetPath,
    [string]$BackupDir
)

function ConvertTo-HashtableRecursive($obj) {
    if ($null -eq $obj) { return $null }
    if ($obj -is [System.Management.Automation.PSCustomObject]) {
        $hash = @{}
        foreach ($prop in $obj.PSObject.Properties) {
            $hash[$prop.Name] = ConvertTo-HashtableRecursive $prop.Value
        }
        return $hash
    }
    if ($obj -is [System.Collections.IEnumerable] -and -not ($obj -is [string])) {
        $arr = @()
        foreach ($item in $obj) { $arr += ConvertTo-HashtableRecursive $item }
        return $arr
    }
    return $obj
}

function Merge-MissingKeys($target, $source) {
    foreach ($key in $source.Keys) {
        if (!$target.ContainsKey($key)) {
            $target[$key] = $source[$key]
        }
        elseif (($target[$key] -is [hashtable]) -and ($source[$key] -is [hashtable])) {
            Merge-MissingKeys $target[$key] $source[$key]
        }
    }
}

if (!(Test-Path $ExamplePath)) { exit 0 }
if (!(Test-Path $TargetPath)) {
    Copy-Item $ExamplePath $TargetPath -Force
    exit 0
}

if (!(Test-Path $BackupDir)) { New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
Copy-Item $TargetPath (Join-Path $BackupDir "localization_before_merge_$stamp.json") -Force

$sourceObj = Get-Content -Raw -Encoding UTF8 $ExamplePath | ConvertFrom-Json
$targetObj = Get-Content -Raw -Encoding UTF8 $TargetPath | ConvertFrom-Json
$source = ConvertTo-HashtableRecursive $sourceObj
$target = ConvertTo-HashtableRecursive $targetObj
Merge-MissingKeys $target $source

$target | ConvertTo-Json -Depth 100 | Set-Content -Encoding UTF8 $TargetPath
