using System.Diagnostics;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using CliWrap;
using DotNetEnv;

const int Port = 8000;
const string AppUrl = "http://127.0.0.1:8000";
const string HealthUrl = $"{AppUrl}/api/health";
Console.Title = "Mount Hosmer Digital Twin Launcher";
var shutdown = new CancellationTokenSource();
CommandTask<CommandResult>? server = null;

ConsoleClose.Register(shutdown.Cancel);
Console.CancelKeyPress += (_, eventArgs) =>
{
    eventArgs.Cancel = true;
    shutdown.Cancel();
};
AppDomain.CurrentDomain.ProcessExit += (_, _) => shutdown.Cancel();

try
{
    var root = FindProjectRoot();
    var envFile = Path.Combine(root, ".env");
    if (File.Exists(envFile))
    {
        Env.NoClobber().Load(envFile);
    }

    var backend = Path.Combine(root, "backend");
    var frontend = Path.Combine(root, "frontend");
    var runtime = Env.GetString(
        "AVALANCHE_RUNTIME_ROOT",
        Env.GetString("MOUNT_HOSMER_RUNTIME_ROOT", Path.Combine(root, "runtime"))
    );
    var data = Env.GetString(
        "AVALANCHE_DATA_ROOT",
        Env.GetString(
            "MOUNT_HOSMER_DATA_ROOT",
            Path.GetFullPath(Path.Combine(root, "..", "DATA", "mount_hosmer_data"))
        )
    );
    var logs = Path.Combine(runtime, "logs");
    Directory.CreateDirectory(logs);

    var environment = new Dictionary<string, string?>
    {
        ["AVALANCHE_DATA_ROOT"] = data,
        ["AVALANCHE_RUNTIME_ROOT"] = runtime,
        ["MOUNT_HOSMER_DATA_ROOT"] = data,
        ["MOUNT_HOSMER_RUNTIME_ROOT"] = runtime,
        ["NEXT_PUBLIC_API_BASE_URL"] = "",
        ["NEXT_PUBLIC_ASSISTANT_BASE_URL"] = ""
    };
    var python = File.Exists(Path.Combine(root, ".venv", "Scripts", "python.exe"))
        ? Path.Combine(root, ".venv", "Scripts", "python.exe")
        : "python";
    var npmName = OperatingSystem.IsWindows() ? "npm.cmd" : "npm";
    var npm = (Environment.GetEnvironmentVariable("PATH") ?? "")
        .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
        .Select(directory => Path.Combine(directory.Trim('"'), npmName))
        .FirstOrDefault(File.Exists) ?? npmName;

    Console.WriteLine("Mount Hosmer Digital Twin");
    Console.WriteLine($"Project: {root}");
    Console.WriteLine($"Runtime: {runtime}\n");

    RequireDirectory(backend, "backend folder");
    RequireDirectory(frontend, "frontend folder");

    if (!File.Exists(Path.Combine(runtime, "baked", "meta.json")))
    {
        RequireDirectory(data, "Mount Hosmer data root");
        Console.WriteLine("Baked terrain is missing; running the one-time bake...");
        await RequireSuccess(
            python, ["-m", "app.bake"], backend, environment,
            Path.Combine(logs, "launcher-bake.log"), TimeSpan.FromMinutes(45)
        );
    }
    else
    {
        Console.WriteLine("Validating baked terrain identity...");
        await RequireSuccess(
            python, ["-m", "app.check_bake"], backend, environment,
            Path.Combine(logs, "launcher-bake-check.log"), TimeSpan.FromMinutes(5)
        );
    }

    if (!Directory.Exists(Path.Combine(frontend, "node_modules")))
    {
        Console.WriteLine("Installing frontend dependencies...");
        await RequireSuccess(
            npm, ["install"], frontend, environment,
            Path.Combine(logs, "launcher-npm-install.log"), TimeSpan.FromMinutes(20)
        );
    }

    Console.WriteLine("Building the local web app...");
    await RequireSuccess(
        npm, ["run", "build"], frontend, environment,
        Path.Combine(logs, "launcher-frontend-build.log"), TimeSpan.FromMinutes(10)
    );

    if (!await IsPortOpen(Port))
    {
        Console.WriteLine($"Starting {AppUrl} ...");
        server = Command(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", Port.ToString()], backend, environment)
            .WithStandardOutputPipe(PipeTarget.ToFile(Path.Combine(logs, "backend.out.log")))
            .WithStandardErrorPipe(PipeTarget.ToFile(Path.Combine(logs, "backend.err.log")))
            .ExecuteAsync(CancellationToken.None, shutdown.Token);
    }
    else
    {
        Console.WriteLine($"Using the service already running on port {Port}.");
    }

    if (!await WaitForHttp(HealthUrl, TimeSpan.FromSeconds(60)) ||
        !await WaitForHttp(AppUrl, TimeSpan.FromSeconds(10)))
    {
        throw new InvalidOperationException("The app did not become ready. See runtime\\logs\\backend.err.log.");
    }

    if (Environment.GetEnvironmentVariable("MOUNT_HOSMER_NO_BROWSER") != "1")
    {
        Process.Start(new ProcessStartInfo(AppUrl) { UseShellExecute = true });
    }

    Console.WriteLine($"\nApp URL: {AppUrl}");
    Console.WriteLine("Press Enter, Ctrl+C, or close this window to stop the app.");
    var input = Task.Run(Console.ReadLine);
    var cancelled = Task.Delay(Timeout.InfiniteTimeSpan, shutdown.Token);
    var stopped = server is null
        ? await Task.WhenAny(input, cancelled)
        : await Task.WhenAny(input, cancelled, server.Task);
    if (server is not null && stopped == server.Task && !shutdown.IsCancellationRequested)
    {
        throw new InvalidOperationException("The backend stopped unexpectedly. See runtime\\logs\\backend.err.log.");
    }

    return 0;
}
catch (OperationCanceledException) when (shutdown.IsCancellationRequested)
{
    return 0;
}
catch (Exception exception)
{
    Console.Error.WriteLine($"\nLauncher failed:\n{exception.Message}");
    if (!Console.IsInputRedirected)
    {
        Console.Error.WriteLine("Press Enter to close this window.");
        Console.ReadLine();
    }
    return 1;
}
finally
{
    shutdown.Cancel();
    if (server is not null)
    {
        try { await server.Task.WaitAsync(TimeSpan.FromSeconds(10)); }
        catch { /* Cancellation intentionally stops the process tree. */ }
    }
}

static Command Command(
    string executable,
    IEnumerable<string> arguments,
    string workingDirectory,
    IReadOnlyDictionary<string, string?> environment) =>
    Cli.Wrap(executable)
        .WithArguments(arguments)
        .WithWorkingDirectory(workingDirectory)
        .WithEnvironmentVariables(environment)
        .WithValidation(CommandResultValidation.None);

static async Task RequireSuccess(
    string executable,
    IEnumerable<string> arguments,
    string workingDirectory,
    IReadOnlyDictionary<string, string?> environment,
    string log,
    TimeSpan timeout)
{
    using var cancellation = new CancellationTokenSource(timeout);
    var console = PipeTarget.ToDelegate(Console.WriteLine);
    var result = await Command(executable, arguments, workingDirectory, environment)
        .WithStandardOutputPipe(PipeTarget.Merge(PipeTarget.ToFile(log), console))
        .WithStandardErrorPipe(PipeTarget.Merge(PipeTarget.ToFile(log + ".err"), PipeTarget.ToDelegate(Console.Error.WriteLine)))
        .ExecuteAsync(cancellation.Token, cancellation.Token);
    if (result.ExitCode != 0)
    {
        throw new InvalidOperationException($"{executable} failed with exit code {result.ExitCode}. See {log}.");
    }
}

static string FindProjectRoot()
{
    foreach (var start in new[] { AppContext.BaseDirectory, Directory.GetCurrentDirectory() })
    {
        var directory = new DirectoryInfo(start);
        for (var depth = 0; directory is not null && depth < 7; depth++, directory = directory.Parent)
        {
            if (File.Exists(Path.Combine(directory.FullName, "backend", "app", "main.py")) &&
                File.Exists(Path.Combine(directory.FullName, "frontend", "package.json")))
            {
                return directory.FullName;
            }
        }
    }
    throw new InvalidOperationException("Could not locate the project root.");
}

static void RequireDirectory(string path, string label)
{
    if (!Directory.Exists(path))
    {
        throw new DirectoryNotFoundException($"Missing {label}: {path}");
    }
}

static async Task<bool> IsPortOpen(int port)
{
    using var client = new TcpClient();
    try
    {
        await client.ConnectAsync("127.0.0.1", port).WaitAsync(TimeSpan.FromMilliseconds(700));
        return true;
    }
    catch { return false; }
}

static async Task<bool> WaitForHttp(string url, TimeSpan timeout)
{
    using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
    using var cancellation = new CancellationTokenSource(timeout);
    while (!cancellation.IsCancellationRequested)
    {
        try
        {
            using var response = await client.GetAsync(url, cancellation.Token);
            if ((int)response.StatusCode < 500) return true;
        }
        catch when (!cancellation.IsCancellationRequested) { }
        try { await Task.Delay(500, cancellation.Token); }
        catch (OperationCanceledException) { }
    }
    return false;
}

static class ConsoleClose
{
    private static ConsoleCtrlDelegate? handler;

    public static void Register(Action cleanup)
    {
        handler = _ => { cleanup(); return false; };
        SetConsoleCtrlHandler(handler, true);
    }

    private delegate bool ConsoleCtrlDelegate(uint controlType);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(ConsoleCtrlDelegate handlerRoutine, bool add);
}
