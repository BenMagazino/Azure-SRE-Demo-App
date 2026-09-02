param(
  [string]$AppUrl = "http://127.0.0.1:8765",
  [int]$TimeoutSeconds = 60,
  [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

[xml]$xaml = @'
<Window
  xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
  xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
  Title="Azure SRE Agent Demo"
  Width="460"
  Height="260"
  WindowStartupLocation="CenterScreen"
  WindowStyle="None"
  ResizeMode="NoResize"
  ShowInTaskbar="True"
  Topmost="True"
  Background="#08111F">
  <Border
    Margin="1"
    Padding="32"
    BorderBrush="#28415F"
    BorderThickness="1"
    CornerRadius="16"
    Background="#0D1A2A">
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto" />
        <RowDefinition Height="*" />
        <RowDefinition Height="Auto" />
      </Grid.RowDefinitions>
      <StackPanel>
        <TextBlock
          Foreground="#4BA3FF"
          FontFamily="Segoe UI"
          FontSize="12"
          FontWeight="Bold"
          Text="GUIDED LABS" />
        <TextBlock
          Margin="0,8,0,0"
          Foreground="#E8F0FA"
          FontFamily="Segoe UI"
          FontSize="25"
          FontWeight="SemiBold"
          Text="Azure SRE Agent Demo" />
      </StackPanel>
      <StackPanel
        Grid.Row="1"
        Margin="0,24,0,0"
        HorizontalAlignment="Center"
        Orientation="Horizontal">
        <Ellipse
          x:Name="Spinner"
          Width="34"
          Height="34"
          Stroke="#4BA3FF"
          StrokeThickness="4"
          StrokeDashArray="1,2"
          StrokeDashCap="Round"
          RenderTransformOrigin="0.5,0.5">
          <Ellipse.RenderTransform>
            <RotateTransform x:Name="SpinnerRotation" />
          </Ellipse.RenderTransform>
          <Ellipse.Triggers>
            <EventTrigger RoutedEvent="Loaded">
              <BeginStoryboard>
                <Storyboard>
                  <DoubleAnimation
                    Storyboard.TargetName="SpinnerRotation"
                    Storyboard.TargetProperty="Angle"
                    From="0"
                    To="360"
                    Duration="0:0:0.8"
                    RepeatBehavior="Forever" />
                </Storyboard>
              </BeginStoryboard>
            </EventTrigger>
          </Ellipse.Triggers>
        </Ellipse>
        <StackPanel
          Width="320"
          Margin="16,0,0,0"
          VerticalAlignment="Center">
          <TextBlock
            x:Name="StatusText"
            Foreground="#E8F0FA"
            FontFamily="Segoe UI"
            FontSize="15"
            Text="Starting the local application..." />
          <TextBlock
            x:Name="DetailText"
            Margin="0,4,0,0"
            Foreground="#9FB0C4"
            FontFamily="Segoe UI"
            FontSize="12"
            TextWrapping="Wrap"
            Text="Preparing the guided lab experience." />
        </StackPanel>
      </StackPanel>
      <Button
        x:Name="CloseButton"
        Grid.Row="2"
        Width="96"
        Padding="12,7"
        HorizontalAlignment="Right"
        BorderBrush="#4BA3FF"
        Background="#15304D"
        Foreground="#E8F0FA"
        FontFamily="Segoe UI"
        Content="Close"
        Visibility="Collapsed" />
    </Grid>
  </Border>
</Window>
'@

$reader = [System.Xml.XmlNodeReader]::new($xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)
$statusText = $window.FindName("StatusText")
$detailText = $window.FindName("DetailText")
$closeButton = $window.FindName("CloseButton")
$spinner = $window.FindName("Spinner")
$iconCandidates = @(
  (Join-Path $PSScriptRoot "Azure SRE Agent Demo.ico"),
  (Join-Path $PSScriptRoot "..\..\app\static\favicon.ico")
)
$iconPath = $iconCandidates |
  Where-Object { Test-Path -LiteralPath $_ } |
  Select-Object -First 1
if ($iconPath) {
  $window.Icon = [Windows.Media.Imaging.BitmapFrame]::Create(
    [Uri]::new((Resolve-Path $iconPath).Path)
  )
}

if ($ValidateOnly) {
  $window.Close()
  exit 0
}

$healthUrl = "$($AppUrl.TrimEnd('/'))/api/health"
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$logDirectory = Join-Path $env:LOCALAPPDATA "AzureSREAgentDemo\logs"
$edgeCandidates = @(
  (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
  (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
  (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
)
$edgePath = $edgeCandidates |
  Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
  Select-Object -First 1
$timer = [Windows.Threading.DispatcherTimer]::new()
$timer.Interval = [TimeSpan]::FromMilliseconds(250)

$closeButton.Add_Click({
  $window.Close()
})
$window.Add_Closed({
  $timer.Stop()
})
$timer.Add_Tick({
  try {
    $request = [Net.HttpWebRequest]::Create($healthUrl)
    $request.Method = "GET"
    $request.Timeout = 300
    $request.ReadWriteTimeout = 300
    $response = $request.GetResponse()
    $ready = $response.StatusCode -eq [Net.HttpStatusCode]::OK
    $response.Close()
    if (-not $ready) {
      return
    }

    $timer.Stop()
    $statusText.Text = "Opening the desktop application..."
    $detailText.Text = "The local backend is ready."
    try {
      if ($edgePath) {
        Start-Process `
          -FilePath $edgePath `
          -ArgumentList @("--app=$AppUrl", "--start-maximized")
      } else {
        Start-Process $AppUrl
      }
      $window.Close()
    } catch {
      $spinner.Visibility = "Collapsed"
      $statusText.Text = "The browser could not be opened."
      $detailText.Text = "Open $AppUrl manually."
      $closeButton.Visibility = "Visible"
      $window.Topmost = $false
    }
  } catch [Net.WebException] {
    if ([DateTime]::UtcNow -lt $deadline) {
      return
    }
    $timer.Stop()
    $spinner.Visibility = "Collapsed"
    $statusText.Text = "The application did not start."
    $detailText.Text = "Review the diagnostic logs in $logDirectory."
    $closeButton.Visibility = "Visible"
    $window.Topmost = $false
  }
})

$timer.Start()
[void]$window.ShowDialog()
