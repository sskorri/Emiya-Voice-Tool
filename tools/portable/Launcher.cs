// Windows GUI launcher. Paths are resolved when the folder is launched, so it can be moved.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    // Encode one argv element for the Windows CreateProcess command line.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int pending = 0;
        foreach (char c in value)
        {
            if (c == '\\') { pending++; continue; }
            result.Append('\\', c == '"' ? 2 * pending + 1 : pending);
            result.Append(c);
            pending = 0;
        }
        return result.Append('\\', 2 * pending).Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool check = args.Length == 1 && args[0] == "--portable-check";
        try
        {
            string script = Path.Combine(root, check ? "portable_check.py" : "gui.py");
            var start = new ProcessStartInfo {
                FileName = Path.Combine(root, "runtime", check ? "python.exe" : "pythonw.exe"),
                WorkingDirectory = root,
                Arguments = Quote(script) + (check ? "" : " " + String.Join(" ", args.Select(Quote))),
                UseShellExecute = false, CreateNoWindow = true,
            };
            using (var child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception e)
        {
            if (!check && !args.Any(a => a.StartsWith("--smoke")) &&
                Environment.GetEnvironmentVariable("VOICETOOL_TEST_WINDOW") != "1")
                MessageBox.Show(e.Message, "Emiya Voice", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
