using System.Diagnostics;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;

const int BackendPort = 8000;
const int FrontendPort = 3000;
const string FrontendUrl = "http://127.0.0.1:3000";
const string BackendHealthUrl = "http://127.0.0.1:8000/api/health";

Console.Title = "Mount Hosmer Digital Twin Launcher";

using var shutdown = new CancellationTokenSource();
var startedProcesses = new ShutdownState();

ConsoleClose.Register(() => StopStartedProcesses(startedProcesses));
Console.CancelKeyPress += (_, args) =>
{
    args.Cancel = true;
    shutdown.Cancel();
};
AppDomain.CurrentDomain.ProcessExit += (_, _) => StopStartedProcesses(startedProcesses);

try
{
    var projectRoot = FindProjectRoot();
    LoadEnvFile(Path.Combine(projectRoot, ".env"));

    var runtimeRoot = GetEnvOrDefault(
        "MOUNT_HOSMER_RUNTIME_ROOT",
        Path.Combine(projectRoot, "runtime")
    );
    var dataRoot = GetEnvOrDefault(
        "MOUNT_HOSMER_DATA_ROOT",
        Path.GetFullPath(Path.Combine(projectRoot, "..", "DATA", "mount_hosmer_data"))
    );
    var backendRoot = Path.Combine(projectRoot, "backend");
    var frontendRoot = Path.Combine(projectRoot, "frontend");
    var logsRoot = Path.Combine(runtimeRoot, "logs");

    Directory.CreateDirectory(runtimeRoot);
    Directory.CreateDirectory(logsRoot);

    var env = new Dictionary<string, string>
    {
        ["MOUNT_HOSMER_DATA_ROOT"] = dataRoot,
        ["MOUNT_HOSMER_RUNTIME_ROOT"] = runtimeRoot,
        ["NEXT_PUBLIC_API_BASE_URL"] = "http://127.0.0.1:8000"
    };

    Console.WriteLine("Mount Hosmer Digital Twin");
    Console.WriteLine("--------------------------");
    Console.WriteLine($"Project: {projectRoot}");
    Console.WriteLine($"Data:    {dataRoot}");
    Console.WriteLine($"Runtime: {runtimeRoot}");
    Console.WriteLine();

    RequireDirectory(projectRoot, "project root");
    RequireDirectory(backendRoot, "backend folder");
    RequireDirectory(frontendRoot, "frontend folder");
    RequireDirectory(dataRoot, "Mount Hosmer data root");

    var python = ResolvePython(projectRoot);
    var npm = ResolveOnPath("npm.cmd") ?? ResolveOnPath("npm") ?? "npm.cmd";
    var node = ResolveOnPath("node.exe") ?? ResolveOnPath("node") ?? "node";

    var catalogPath = Path.Combine(runtimeRoot, "catalog", "data_catalog.json");
    if (!File.Exists(catalogPath))
    {
        Console.WriteLine("Catalog is missing. Running a quick metadata scan...");
        var exitCode = RunAndLog(
            python,
            "-m app.cli scan-data --skip-checksum",
            backendRoot,
            env,
            Path.Combine(logsRoot, "launcher-scan.out.log"),
            Path.Combine(logsRoot, "launcher-scan.err.log"),
            TimeSpan.FromMinutes(15)
        );
        if (exitCode != 0)
        {
            return Fail($"Catalog scan failed with exit code {exitCode}. See runtime\\logs\\launcher-scan.err.log.");
        }
    }

    var nodeModules = Path.Combine(frontendRoot, "node_modules");
    if (!Directory.Exists(nodeModules))
    {
        Console.WriteLine("Frontend dependencies are missing. Running npm install...");
        var exitCode = RunAndLog(
            npm,
            "install",
            frontendRoot,
            env,
            Path.Combine(logsRoot, "launcher-npm-install.out.log"),
            Path.Combine(logsRoot, "launcher-npm-install.err.log"),
            TimeSpan.FromMinutes(20)
        );
        if (exitCode != 0)
        {
            return Fail($"npm install failed with exit code {exitCode}. See runtime\\logs\\launcher-npm-install.err.log.");
        }
    }
    var nextCli = Path.Combine(frontendRoot, "node_modules", "next", "dist", "bin", "next");
    RequireFile(nextCli, "Next.js CLI");

    if (await IsPortOpenAsync(BackendPort))
    {
        Console.WriteLine("Backend is already running on port 8000.");
    }
    else
    {
        Console.WriteLine("Starting backend on http://127.0.0.1:8000 ...");
        startedProcesses.Add(StartManaged(
            "backend",
            python,
            "-m uvicorn app.main:app --host 127.0.0.1 --port 8000",
            backendRoot,
            env,
            Path.Combine(logsRoot, "backend.out.log"),
            Path.Combine(logsRoot, "backend.err.log")
        ));
    }

    if (!await WaitForHttpAsync(BackendHealthUrl, TimeSpan.FromSeconds(60)))
    {
        return Fail("Backend did not become ready. See runtime\\logs\\backend.err.log.");
    }

    if (await IsPortOpenAsync(FrontendPort))
    {
        Console.WriteLine("Frontend is already running on port 3000.");
    }
    else
    {
        Console.WriteLine("Starting frontend on http://127.0.0.1:3000 ...");
        startedProcesses.Add(StartManaged(
            "frontend",
            node,
            $"\"{nextCli}\" dev --hostname 127.0.0.1 --port 3000",
            frontendRoot,
            env,
            Path.Combine(logsRoot, "frontend.out.log"),
            Path.Combine(logsRoot, "frontend.err.log")
        ));
    }

    if (!await WaitForHttpAsync(FrontendUrl, TimeSpan.FromSeconds(90)))
    {
        return Fail("Frontend did not become ready. See runtime\\logs\\frontend.err.log.");
    }

    Console.WriteLine();
    if (string.Equals(Environment.GetEnvironmentVariable("MOUNT_HOSMER_NO_BROWSER"), "1", StringComparison.Ordinal))
    {
        Console.WriteLine("Browser opening skipped by MOUNT_HOSMER_NO_BROWSER=1.");
    }
    else
    {
        Console.WriteLine("Opening app in your browser...");
        Process.Start(new ProcessStartInfo(FrontendUrl) { UseShellExecute = true });
    }
    Console.WriteLine($"App URL: {FrontendUrl}");
    Console.WriteLine();
    Console.WriteLine("Leave this window open while using the app.");
    Console.WriteLine("Press Enter in this window, press Ctrl+C, or close this window to stop the app.");

    await WaitForShutdownAsync(shutdown.Token);
    Console.WriteLine();
    Console.WriteLine("Stopping app...");
    return 0;
}
catch (Exception ex)
{
    return Fail(ex.Message);
}
finally
{
    StopStartedProcesses(startedProcesses);
}

static string FindProjectRoot()
{
    var candidates = new[]
    {
        AppContext.BaseDirectory,
        Directory.GetCurrentDirectory(),
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..")),
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..")),
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..")),
    };

    foreach (var candidate in candidates.Distinct(StringComparer.OrdinalIgnoreCase))
    {
        if (File.Exists(Path.Combine(candidate, "backend", "app", "main.py")) &&
            File.Exists(Path.Combine(candidate, "frontend", "package.json")))
        {
            return Path.GetFullPath(candidate);
        }
    }

    throw new InvalidOperationException("Could not locate the project root. Place this executable in the mount-hosmer-digital-twin folder.");
}

static void LoadEnvFile(string path)
{
    if (!File.Exists(path))
    {
        return;
    }

    foreach (var rawLine in File.ReadAllLines(path))
    {
        var line = rawLine.Trim();
        if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal) || !line.Contains('='))
        {
            continue;
        }

        var parts = line.Split('=', 2);
        var key = parts[0].Trim();
        var value = parts[1].Trim().Trim('"', '\'');
        if (!string.IsNullOrWhiteSpace(key) && string.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)))
        {
            Environment.SetEnvironmentVariable(key, value);
        }
    }
}

static string GetEnvOrDefault(string name, string fallback)
{
    var value = Environment.GetEnvironmentVariable(name);
    return string.IsNullOrWhiteSpace(value) ? fallback : value;
}

static string ResolvePython(string projectRoot)
{
    var venvPython = Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
    return File.Exists(venvPython) ? venvPython : "python";
}

static string? ResolveOnPath(string command)
{
    try
    {
        using var process = new Process();
        process.StartInfo = new ProcessStartInfo
        {
            FileName = "where.exe",
            Arguments = command,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true
        };
        process.Start();
        var output = process.StandardOutput.ReadToEnd();
        process.WaitForExit(3000);
        if (process.ExitCode != 0)
        {
            return null;
        }
        return output
            .Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
            .FirstOrDefault(File.Exists);
    }
    catch
    {
        return null;
    }
}

static void RequireDirectory(string path, string label)
{
    if (!Directory.Exists(path))
    {
        throw new DirectoryNotFoundException($"Missing {label}: {path}");
    }
}

static void RequireFile(string path, string label)
{
    if (!File.Exists(path))
    {
        throw new FileNotFoundException($"Missing {label}: {path}", path);
    }
}

static StartedProcess StartManaged(
    string label,
    string executable,
    string arguments,
    string workingDirectory,
    IReadOnlyDictionary<string, string> environment,
    string stdoutLog,
    string stderrLog)
{
    Directory.CreateDirectory(Path.GetDirectoryName(stdoutLog)!);
    Directory.CreateDirectory(Path.GetDirectoryName(stderrLog)!);

    var stdout = new StreamWriter(stdoutLog, append: true) { AutoFlush = true };
    var stderr = new StreamWriter(stderrLog, append: true) { AutoFlush = true };
    var stdoutGate = new object();
    var stderrGate = new object();

    var process = new Process();
    process.StartInfo = new ProcessStartInfo
    {
        FileName = executable,
        Arguments = arguments,
        WorkingDirectory = workingDirectory,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true
    };
    foreach (var pair in environment)
    {
        process.StartInfo.Environment[pair.Key] = pair.Value;
    }
    process.OutputDataReceived += (_, args) =>
    {
        if (args.Data is not null)
        {
            lock (stdoutGate)
            {
                stdout.WriteLine(args.Data);
            }
        }
    };
    process.ErrorDataReceived += (_, args) =>
    {
        if (args.Data is not null)
        {
            lock (stderrGate)
            {
                stderr.WriteLine(args.Data);
            }
        }
    };

    if (!process.Start())
    {
        stdout.Dispose();
        stderr.Dispose();
        process.Dispose();
        throw new InvalidOperationException($"Failed to start {label}.");
    }
    process.BeginOutputReadLine();
    process.BeginErrorReadLine();
    return new StartedProcess(label, process, stdout, stderr);
}

static int RunAndLog(
    string executable,
    string arguments,
    string workingDirectory,
    IReadOnlyDictionary<string, string> environment,
    string stdoutLog,
    string stderrLog,
    TimeSpan timeout)
{
    Directory.CreateDirectory(Path.GetDirectoryName(stdoutLog)!);
    Directory.CreateDirectory(Path.GetDirectoryName(stderrLog)!);

    using var stdout = new StreamWriter(stdoutLog, append: true) { AutoFlush = true };
    using var stderr = new StreamWriter(stderrLog, append: true) { AutoFlush = true };
    using var process = new Process();
    process.StartInfo = new ProcessStartInfo
    {
        FileName = executable,
        Arguments = arguments,
        WorkingDirectory = workingDirectory,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true
    };
    foreach (var pair in environment)
    {
        process.StartInfo.Environment[pair.Key] = pair.Value;
    }
    process.OutputDataReceived += (_, args) =>
    {
        if (args.Data is not null)
        {
            stdout.WriteLine(args.Data);
            Console.WriteLine(args.Data);
        }
    };
    process.ErrorDataReceived += (_, args) =>
    {
        if (args.Data is not null)
        {
            stderr.WriteLine(args.Data);
            Console.Error.WriteLine(args.Data);
        }
    };

    process.Start();
    process.BeginOutputReadLine();
    process.BeginErrorReadLine();
    if (!process.WaitForExit((int)timeout.TotalMilliseconds))
    {
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch
        {
            // Process may already have exited.
        }
        return -1;
    }
    return process.ExitCode;
}

static async Task WaitForShutdownAsync(CancellationToken cancellationToken)
{
    var enterTask = Task.Run(Console.ReadLine, cancellationToken);
    var cancelTask = Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
    await Task.WhenAny(enterTask, cancelTask);
}

static async Task<bool> IsPortOpenAsync(int port)
{
    using var client = new TcpClient();
    var connectTask = client.ConnectAsync("127.0.0.1", port);
    var completed = await Task.WhenAny(connectTask, Task.Delay(700));
    return completed == connectTask && client.Connected;
}

static async Task<bool> WaitForHttpAsync(string url, TimeSpan timeout)
{
    using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
    var deadline = DateTimeOffset.UtcNow + timeout;
    while (DateTimeOffset.UtcNow < deadline)
    {
        try
        {
            using var response = await client.GetAsync(url);
            if ((int)response.StatusCode < 500)
            {
                Console.WriteLine($"Ready: {url}");
                return true;
            }
        }
        catch
        {
            // Server is still starting.
        }
        await Task.Delay(1000);
    }
    return false;
}

static void StopStartedProcesses(ShutdownState state)
{
    var processes = state.BeginCleanup();
    foreach (var started in processes.Reverse())
    {
        StopProcess(started);
    }
}

static void StopProcess(StartedProcess started)
{
    try
    {
        if (started.Process.HasExited)
        {
            return;
        }

        Console.WriteLine($"Stopping {started.Label}...");
        started.Process.Kill(entireProcessTree: true);
        if (started.Process.WaitForExit(10000))
        {
            started.Process.WaitForExit();
        }
        else
        {
            Console.Error.WriteLine($"{started.Label} did not exit within 10 seconds.");
        }
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"Could not stop {started.Label}: {ex.Message}");
    }
    finally
    {
        started.Process.Dispose();
        started.Stdout?.Dispose();
        started.Stderr?.Dispose();
    }
}

static int Fail(string message)
{
    Console.Error.WriteLine();
    Console.Error.WriteLine("Launcher failed:");
    Console.Error.WriteLine(message);
    Console.Error.WriteLine();
    Console.Error.WriteLine("Press Enter to close this window.");
    Console.ReadLine();
    return 1;
}

sealed class ShutdownState
{
    private readonly object gate = new();
    private readonly List<StartedProcess> processes = new();
    private bool cleanupStarted;

    public void Add(StartedProcess process)
    {
        var stopImmediately = false;
        lock (gate)
        {
            if (cleanupStarted)
            {
                stopImmediately = true;
            }
            else
            {
                processes.Add(process);
            }
        }

        if (stopImmediately)
        {
            try
            {
                if (!process.Process.HasExited)
                {
                    process.Process.Kill(entireProcessTree: true);
                    process.Process.WaitForExit(10000);
                }
            }
            catch
            {
                // Shutdown is already in progress.
            }
            finally
            {
                process.Process.Dispose();
                process.Stdout?.Dispose();
                process.Stderr?.Dispose();
            }
        }
    }

    public StartedProcess[] BeginCleanup()
    {
        lock (gate)
        {
            if (cleanupStarted)
            {
                return Array.Empty<StartedProcess>();
            }

            cleanupStarted = true;
            return processes.ToArray();
        }
    }
}

sealed record StartedProcess(string Label, Process Process, StreamWriter? Stdout, StreamWriter? Stderr);

static class ConsoleClose
{
    private static ConsoleCtrlDelegate? handler;

    public static void Register(Action cleanup)
    {
        handler = _ =>
        {
            cleanup();
            return false;
        };
        SetConsoleCtrlHandler(handler, true);
    }

    private delegate bool ConsoleCtrlDelegate(uint ctrlType);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(ConsoleCtrlDelegate handlerRoutine, bool add);
}
