using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace MLACStudioBootstrap
{
    internal sealed class ProgressWindow : Form
    {
        private readonly Label status = new Label();
        private readonly ProgressBar progress = new ProgressBar();

        internal ProgressWindow()
        {
            Text = "MLAC Studio Setup";
            Width = 520;
            Height = 145;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            status.Left = 20;
            status.Top = 18;
            status.Width = 465;
            status.Height = 38;
            status.Text = "Preparing MLAC Studio...";
            progress.Left = 20;
            progress.Top = 66;
            progress.Width = 465;
            progress.Height = 22;
            Controls.Add(status);
            Controls.Add(progress);
        }

        internal void Report(string message, long completed, long total)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string, long, long>(Report), message, completed, total);
                return;
            }
            status.Text = message;
            if (total <= 0)
            {
                progress.Style = ProgressBarStyle.Marquee;
            }
            else
            {
                progress.Style = ProgressBarStyle.Continuous;
                progress.Minimum = 0;
                progress.Maximum = 1000;
                progress.Value = (int)Math.Max(0, Math.Min(1000, completed * 1000L / total));
            }
            Application.DoEvents();
        }
    }

    internal static class Program
    {
        private const string Version = "0.3.0";
        private const string KeyId = "mlac-release-2026-01";
        private const string PublicKeyXml = "<RSAKeyValue><Modulus>zeZbJnjO02luXHUF2vbwpXccSUdYZ8Kaqa7z9HkkdueKNvna1JAOvpXsFWY5XFt15V7+eHadFQQZvOiJ1uiNMGukHsyO2uO4NLcRONL8VHKseIY5ECKZnnWJVKUtiPbLewg1sVNHw+AyWYGU2ZmOcCe7UDOjo+1vg/wzQveUcaneJLK2Ps3yWQPS1YvPms7Fj2a/H+X7wfPVO3HtlbbDv/1r4CqjSJnEnjcthQjGUN08fTbT56Y3VoJCagqyKZRTcA9gICz+1QZYjRsuQww5eeyajs2SRfwcS07pWdjcJl05DLd+RDzowf2h7Ep2DmtyCWfpUE5YOEOT6yDQkTIzJn133pyGZef6jB7sk3vcFYTZfW1DThkkdyoIFD06xfzTJKl/UDOOREN+BGfjTaLEQTcZpq5xyJBXldA6tTsFGuZAZhcrdGnPvvfOXGhz6ICceoSAqhE/aGuYZIC/9FoXIy5O8RAx0/xWv9u2Nb0VYrIPQbxUcTQYqk9xndSxwXkR</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>";
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
        private static ProgressWindow window;

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                Dictionary<string, string> options = ParseArgs(args);
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string stateRoot = GetOption(options, "state-root", Path.Combine(local, "MLACStudio"));
                string installRoot = GetOption(options, "install-root", Path.Combine(local, "Programs", "MLACStudio"));
                Directory.CreateDirectory(stateRoot);
                Directory.CreateDirectory(installRoot);
                if (!options.ContainsKey("verify-only"))
                {
                    InstallSelf(installRoot);
                    MigrateLegacy(stateRoot, installRoot);
                }

                if (options.ContainsKey("launch"))
                {
                    LaunchActive(stateRoot, args);
                    return 0;
                }
                if (options.ContainsKey("rollback"))
                {
                    Rollback(stateRoot);
                    LaunchActive(stateRoot, new string[0]);
                    return 0;
                }

                window = new ProgressWindow();
                window.Show();
                string channel = GetOption(options, "channel", "stable");
                if (channel != "stable" && channel != "beta" && channel != "dev")
                    throw new InvalidOperationException("Invalid update channel.");
                string manifestUrl = GetOption(options, "manifest", "https://github.com/vuisme/mlacstudio/releases/latest/download/MLAC-Studio-" + channel + ".json");
                string manifestText = options.ContainsKey("manifest-file")
                    ? File.ReadAllText(options["manifest-file"], Encoding.UTF8)
                    : DownloadText(manifestUrl);
                Dictionary<string, object> payload = VerifyEnvelope(manifestText);
                if (Convert.ToString(payload["channel"], CultureInfo.InvariantCulture) != channel)
                    throw new InvalidOperationException("Signed update channel does not match the requested channel.");
                if (options.ContainsKey("verify-only"))
                {
                    window.Close();
                    return 0;
                }
                if (payload.ContainsKey("data_migration") && payload["data_migration"] != null && !options.ContainsKey("approve-data-migration"))
                {
                    DialogResult answer = MessageBox.Show(
                        "This signed update requires a user-data/database migration:\n\n" + Convert.ToString(payload["data_migration"]) +
                        "\n\nContinue only after making a backup.", "MLAC Studio Update", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                    if (answer != DialogResult.Yes) return 2;
                }
                Install(payload, stateRoot, options.ContainsKey("explicit-rollback"));
                CreateShortcuts(installRoot);
                window.Close();
                LaunchActive(stateRoot, new string[0]);
                return 0;
            }
            catch (Exception ex)
            {
                if (window != null) window.Close();
                MessageBox.Show(ex.Message, "MLAC Studio Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        private static Dictionary<string, string> ParseArgs(string[] args)
        {
            Dictionary<string, string> result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int index = 0; index < args.Length; index++)
            {
                if (!args[index].StartsWith("--", StringComparison.Ordinal)) continue;
                string name = args[index].Substring(2);
                string value = "true";
                if (index + 1 < args.Length && !args[index + 1].StartsWith("--", StringComparison.Ordinal)) value = args[++index];
                result[name] = value;
            }
            return result;
        }

        private static string GetOption(Dictionary<string, string> options, string name, string fallback)
        {
            string value;
            return options.TryGetValue(name, out value) ? value : fallback;
        }

        private static Dictionary<string, object> VerifyEnvelope(string text)
        {
            Dictionary<string, object> envelope = Json.Deserialize<Dictionary<string, object>>(text);
            if (envelope == null || !envelope.ContainsKey("payload") || !envelope.ContainsKey("signature"))
                throw new CryptographicException("Update metadata is unsigned.");
            Dictionary<string, object> payload = envelope["payload"] as Dictionary<string, object>;
            Dictionary<string, object> signature = envelope["signature"] as Dictionary<string, object>;
            if (payload == null || signature == null || Convert.ToString(signature["algorithm"]) != "rsa-sha256" || Convert.ToString(signature["key_id"]) != KeyId)
                throw new CryptographicException("Update metadata signature is invalid.");
            byte[] rawSignature = Convert.FromBase64String(Convert.ToString(signature["value"]));
            byte[] canonical = Encoding.UTF8.GetBytes(Canonical(payload));
            using (RSACryptoServiceProvider rsa = new RSACryptoServiceProvider())
            using (SHA256 sha = SHA256.Create())
            {
                rsa.FromXmlString(PublicKeyXml);
                if (!rsa.VerifyData(canonical, sha, rawSignature))
                    throw new CryptographicException("Update metadata signature is invalid.");
            }
            ValidatePayload(payload);
            return payload;
        }

        private static string Canonical(object value)
        {
            if (value == null) return "null";
            Dictionary<string, object> map = value as Dictionary<string, object>;
            if (map != null)
            {
                List<string> keys = new List<string>(map.Keys);
                keys.Sort(StringComparer.Ordinal);
                List<string> pairs = new List<string>();
                foreach (string key in keys) pairs.Add(Json.Serialize(key) + ":" + Canonical(map[key]));
                return "{" + string.Join(",", pairs.ToArray()) + "}";
            }
            ArrayList array = value as ArrayList;
            if (array != null)
            {
                List<string> items = new List<string>();
                foreach (object item in array) items.Add(Canonical(item));
                return "[" + string.Join(",", items.ToArray()) + "]";
            }
            if (value is string) return Json.Serialize(value);
            if (value is bool) return (bool)value ? "true" : "false";
            return Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        private static void ValidatePayload(Dictionary<string, object> payload)
        {
            if (Convert.ToInt32(payload["schema_version"], CultureInfo.InvariantCulture) != 1)
                throw new InvalidOperationException("Unsupported update metadata schema.");
            ArrayList components = payload["components"] as ArrayList;
            if (components == null || components.Count == 0) throw new InvalidOperationException("Update metadata has no components.");
            foreach (object raw in components)
            {
                Dictionary<string, object> component = raw as Dictionary<string, object>;
                string id = Convert.ToString(component["id"]);
                string kind = Convert.ToString(component["kind"]);
                string url = Convert.ToString(component["url"]);
                string hash = Convert.ToString(component["sha256"]);
                if (id.ToLowerInvariant().Contains("model") || (kind != "core" && kind != "common-runtime" && kind != "nvidia-runtime"))
                    throw new InvalidOperationException("Invalid update component: " + id);
                if (!url.StartsWith("https://github.com/vuisme/mlacstudio/releases/download/", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Component is not a public MLAC Studio GitHub release asset: " + id);
                if (hash.Length != 64 || Convert.ToInt64(component["size"], CultureInfo.InvariantCulture) <= 0)
                    throw new InvalidOperationException("Invalid component integrity metadata: " + id);
            }
        }

        private static string DownloadText(string url)
        {
            window.Report("Checking signed update metadata...", 0, 0);
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = "MLACStudioBootstrap/" + Version;
            request.Timeout = 15000;
            request.ReadWriteTimeout = 15000;
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                return reader.ReadToEnd();
        }

        private static ArrayList SelectComponents(Dictionary<string, object> payload)
        {
            ArrayList selected = new ArrayList();
            Dictionary<string, object> gpu = DetectGpu();
            foreach (object raw in (ArrayList)payload["components"])
            {
                Dictionary<string, object> component = (Dictionary<string, object>)raw;
                string kind = Convert.ToString(component["kind"]);
                if (kind == "core" || kind == "common-runtime") selected.Add(component);
                else if (kind == "nvidia-runtime" && gpu != null)
                {
                    int minimum = component.ContainsKey("min_driver_major") ? Convert.ToInt32(component["min_driver_major"]) : 0;
                    if (Convert.ToInt32(gpu["driver_major"]) >= minimum) selected.Add(component);
                }
            }
            return selected;
        }

        private static Dictionary<string, object> DetectGpu()
        {
            try
            {
                ProcessStartInfo start = new ProcessStartInfo("nvidia-smi", "--query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits");
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden;
                start.RedirectStandardOutput = true;
                using (Process process = Process.Start(start))
                {
                    string line = process.StandardOutput.ReadLine();
                    if (!process.WaitForExit(10000) || String.IsNullOrWhiteSpace(line)) return null;
                    string[] parts = line.Split(',');
                    return new Dictionary<string, object> {
                        { "name", parts[0].Trim() }, { "vram_mib", Int32.Parse(parts[1].Trim()) },
                        { "driver_major", Int32.Parse(parts[2].Trim().Split('.')[0]) }
                    };
                }
            }
            catch { return null; }
        }

        private static void Install(Dictionary<string, object> payload, string stateRoot, bool explicitRollback)
        {
            string componentRoot = Path.Combine(stateRoot, "components");
            Directory.CreateDirectory(componentRoot);
            Recover(componentRoot);
            Dictionary<string, object> active = ReadObject(Path.Combine(componentRoot, "active.json"));
            string currentVersion = active.ContainsKey("app_version") ? Convert.ToString(active["app_version"]) : "0.0.0";
            string targetVersion = Convert.ToString(payload["version"]);
            if (!explicitRollback && CompareVersions(targetVersion, currentVersion) < 0)
                throw new InvalidOperationException("Downgrade rejected. Use the explicit rollback action.");
            ArrayList selected = SelectComponents(payload);
            Dictionary<string, object> activeComponents = active.ContainsKey("components")
                ? (Dictionary<string, object>)active["components"] : new Dictionary<string, object>();
            ArrayList changed = new ArrayList();
            long required = 0;
            foreach (Dictionary<string, object> component in selected)
            {
                string id = Convert.ToString(component["id"]);
                string hash = Convert.ToString(component["sha256"]);
                Dictionary<string, object> entry = activeComponents.ContainsKey(id) ? activeComponents[id] as Dictionary<string, object> : null;
                Dictionary<string, object> current = entry != null && entry.ContainsKey("active") ? entry["active"] as Dictionary<string, object> : null;
                if (current == null || Convert.ToString(current["sha256"]) != hash)
                {
                    changed.Add(component);
                    required += Convert.ToInt64(component["size"], CultureInfo.InvariantCulture);
                }
            }
            EnsureDiskSpace(componentRoot, required);
            List<KeyValuePair<Dictionary<string, object>, string>> staged = new List<KeyValuePair<Dictionary<string, object>, string>>();
            foreach (Dictionary<string, object> component in changed)
            {
                string id = Convert.ToString(component["id"]);
                string hash = Convert.ToString(component["sha256"]);
                string download = Path.Combine(stateRoot, "updates", "downloads", id + "-" + hash + ".zip.part");
                DownloadComponent(component, download);
                string stage = Path.Combine(componentRoot, ".staging", id + "-" + hash);
                if (Directory.Exists(stage)) Directory.Delete(stage, true);
                Directory.CreateDirectory(stage);
                ExtractSafe(download, stage);
                staged.Add(new KeyValuePair<Dictionary<string, object>, string>(component, stage));
            }
            WriteAtomic(Path.Combine(componentRoot, "activation-journal.json"), Json.Serialize(new Dictionary<string, object> { { "prior", active } }));
            active["schema_version"] = 1;
            active["app_version"] = targetVersion;
            active["channel"] = payload["channel"];
            active["components"] = activeComponents;
            foreach (KeyValuePair<Dictionary<string, object>, string> pair in staged)
            {
                Dictionary<string, object> component = pair.Key;
                string id = Convert.ToString(component["id"]);
                string hash = Convert.ToString(component["sha256"]);
                string componentVersion = component.ContainsKey("version") ? Convert.ToString(component["version"]) : targetVersion;
                string destination = Path.Combine(componentRoot, id, componentVersion + "-" + hash.Substring(0, 16));
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                if (Directory.Exists(destination)) Directory.Delete(pair.Value, true); else Directory.Move(pair.Value, destination);
                Dictionary<string, object> oldEntry = activeComponents.ContainsKey(id) ? activeComponents[id] as Dictionary<string, object> : null;
                object previous = oldEntry != null && oldEntry.ContainsKey("active") ? oldEntry["active"] : null;
                activeComponents[id] = new Dictionary<string, object> {
                    { "active", new Dictionary<string, object> { { "version", componentVersion }, { "sha256", hash }, { "path", destination } } },
                    { "previous", previous }
                };
            }
            WriteAtomic(Path.Combine(componentRoot, "active.json"), Json.Serialize(active));
            File.Delete(Path.Combine(componentRoot, "activation-journal.json"));
            Prune(componentRoot, activeComponents);
        }

        private static void DownloadComponent(Dictionary<string, object> component, string destination)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(destination));
            long size = Convert.ToInt64(component["size"], CultureInfo.InvariantCulture);
            string id = Convert.ToString(component["id"]);
            Exception last = null;
            for (int attempt = 0; attempt < 3; attempt++)
            {
                try
                {
                    long offset = File.Exists(destination) ? new FileInfo(destination).Length : 0;
                    if (offset > size) { File.Delete(destination); offset = 0; }
                    HttpWebRequest request = (HttpWebRequest)WebRequest.Create(Convert.ToString(component["url"]));
                    request.UserAgent = "MLACStudioBootstrap/" + Version;
                    request.Timeout = 30000;
                    request.ReadWriteTimeout = 30000;
                    if (offset > 0) request.AddRange(offset);
                    using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    {
                        if (offset > 0 && response.StatusCode != HttpStatusCode.PartialContent) { File.Delete(destination); offset = 0; }
                        using (Stream input = response.GetResponseStream())
                        using (FileStream output = new FileStream(destination, offset > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None))
                        {
                            byte[] buffer = new byte[1024 * 1024];
                            int read;
                            long complete = offset;
                            while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                            {
                                output.Write(buffer, 0, read);
                                complete += read;
                                window.Report("Downloading " + id + "...", complete, size);
                            }
                        }
                    }
                    if (new FileInfo(destination).Length != size || HashFile(destination) != Convert.ToString(component["sha256"]).ToLowerInvariant())
                    {
                        File.Delete(destination);
                        throw new InvalidDataException("Component integrity check failed: " + id);
                    }
                    return;
                }
                catch (Exception ex) { last = ex; Thread.Sleep(Math.Min(4000, 1000 << attempt)); }
            }
            throw new InvalidOperationException("Download failed after retries: " + id, last);
        }

        private static string HashFile(string path)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
            {
                StringBuilder value = new StringBuilder();
                foreach (byte item in sha.ComputeHash(stream)) value.Append(item.ToString("x2"));
                return value.ToString();
            }
        }

        private static void ExtractSafe(string archive, string destination)
        {
            string root = Path.GetFullPath(destination) + Path.DirectorySeparatorChar;
            using (ZipArchive zip = ZipFile.OpenRead(archive))
            {
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    string target = Path.GetFullPath(Path.Combine(destination, entry.FullName));
                    if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Archive path traversal rejected.");
                    string lower = entry.FullName.ToLowerInvariant();
                    if (lower.Contains("/models/") || lower.EndsWith(".gguf") || lower.EndsWith(".safetensors") || lower.EndsWith(".ckpt") || lower.EndsWith(".pt") || lower.EndsWith(".pth"))
                        throw new InvalidDataException("Model weights are forbidden in update components.");
                    if (String.IsNullOrEmpty(entry.Name)) Directory.CreateDirectory(target);
                    else { Directory.CreateDirectory(Path.GetDirectoryName(target)); entry.ExtractToFile(target, true); }
                }
            }
        }

        private static void EnsureDiskSpace(string root, long required)
        {
            DriveInfo drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(root)));
            long reserve = Math.Max(256L * 1024 * 1024, required / 10);
            if (drive.AvailableFreeSpace < required + reserve) throw new IOException("Not enough disk space for the selected components.");
        }

        private static void Recover(string componentRoot)
        {
            string journalPath = Path.Combine(componentRoot, "activation-journal.json");
            if (!File.Exists(journalPath)) return;
            Dictionary<string, object> journal = ReadObject(journalPath);
            if (journal.ContainsKey("prior")) WriteAtomic(Path.Combine(componentRoot, "active.json"), Json.Serialize(journal["prior"]));
            File.Delete(journalPath);
        }

        private static void Rollback(string stateRoot)
        {
            string path = Path.Combine(stateRoot, "components", "active.json");
            Dictionary<string, object> active = ReadObject(path);
            Dictionary<string, object> components = (Dictionary<string, object>)active["components"];
            bool changed = false;
            foreach (string id in new List<string>(components.Keys))
            {
                Dictionary<string, object> entry = components[id] as Dictionary<string, object>;
                if (entry != null && entry.ContainsKey("previous") && entry["previous"] != null)
                {
                    object current = entry["active"];
                    entry["active"] = entry["previous"];
                    entry["previous"] = current;
                    changed = true;
                }
            }
            if (!changed) throw new InvalidOperationException("No previous component version is available.");
            WriteAtomic(path, Json.Serialize(active));
        }

        private static void Prune(string componentRoot, Dictionary<string, object> components)
        {
            foreach (KeyValuePair<string, object> item in components)
            {
                Dictionary<string, object> entry = item.Value as Dictionary<string, object>;
                HashSet<string> keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (string slot in new string[] { "active", "previous" })
                {
                    Dictionary<string, object> pointer = entry != null && entry.ContainsKey(slot) ? entry[slot] as Dictionary<string, object> : null;
                    if (pointer != null) keep.Add(Path.GetFullPath(Convert.ToString(pointer["path"])));
                }
                string directory = Path.Combine(componentRoot, item.Key);
                if (!Directory.Exists(directory)) continue;
                foreach (string child in Directory.GetDirectories(directory)) if (!keep.Contains(Path.GetFullPath(child))) Directory.Delete(child, true);
            }
        }

        private static Dictionary<string, object> ReadObject(string path)
        {
            if (!File.Exists(path)) return new Dictionary<string, object> { { "schema_version", 1 }, { "app_version", "0.0.0" }, { "components", new Dictionary<string, object>() } };
            return Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
        }

        private static void WriteAtomic(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string temporary = path + ".tmp";
            File.WriteAllText(temporary, text + Environment.NewLine, new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
        }

        private static int CompareVersions(string left, string right)
        {
            Version a, b;
            if (!System.Version.TryParse(left.Split('-')[0], out a) || !System.Version.TryParse(right.Split('-')[0], out b))
                throw new InvalidOperationException("Invalid semantic version in update metadata.");
            return a.CompareTo(b);
        }

        private static void InstallSelf(string installRoot)
        {
            string source = Application.ExecutablePath;
            string target = Path.Combine(installRoot, "MLACStudioBootstrap.exe");
            if (!String.Equals(Path.GetFullPath(source), Path.GetFullPath(target), StringComparison.OrdinalIgnoreCase)) File.Copy(source, target, true);
        }

        private static void CreateShortcuts(string installRoot)
        {
            string target = Path.Combine(installRoot, "MLACStudioBootstrap.exe");
            string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
            string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            CreateShortcut(Path.Combine(programs, "MLAC Studio.lnk"), target);
            CreateShortcut(Path.Combine(desktop, "MLAC Studio.lnk"), target);
        }

        private static void CreateShortcut(string shortcut, string target)
        {
            string script = "$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut('" + shortcut.Replace("'", "''") + "');$s.TargetPath='" + target.Replace("'", "''") + "';$s.Arguments='--launch';$s.WorkingDirectory='" + Path.GetDirectoryName(target).Replace("'", "''") + "';$s.Save()";
            ProcessStartInfo start = new ProcessStartInfo("powershell.exe", "-NoProfile -NonInteractive -WindowStyle Hidden -Command \"" + script.Replace("\"", "\\\"") + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            using (Process process = Process.Start(start)) process.WaitForExit(15000);
        }

        private static void LaunchActive(string stateRoot, string[] originalArgs)
        {
            Dictionary<string, object> active = ReadObject(Path.Combine(stateRoot, "components", "active.json"));
            Dictionary<string, object> components = active.ContainsKey("components") ? active["components"] as Dictionary<string, object> : null;
            if (components == null || !components.ContainsKey("core")) throw new InvalidOperationException("MLAC Studio core is not installed.");
            Dictionary<string, object> core = components["core"] as Dictionary<string, object>;
            Dictionary<string, object> pointer = core["active"] as Dictionary<string, object>;
            string executable = Path.Combine(Convert.ToString(pointer["path"]), "MLACStudio.exe");
            if (!File.Exists(executable)) throw new FileNotFoundException("Active MLAC Studio executable is missing.", executable);
            ProcessStartInfo start = new ProcessStartInfo(executable);
            start.UseShellExecute = true;
            if (Array.IndexOf(originalArgs, "--startup") >= 0) start.Arguments = "--startup";
            Process.Start(start);
        }

        private static void MigrateLegacy(string stateRoot, string installRoot)
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string legacy = Path.Combine(local, "QwenImageStudio");
            string marker = Path.Combine(stateRoot, ".legacy-migration-v1.json");
            if (!Directory.Exists(legacy) || File.Exists(marker)) return;
            foreach (string name in new string[] { "config.json", "model-manager-state.json", "installed-profiles.json", "license-acceptance.json", "hf-source.json" })
            {
                string source = Path.Combine(legacy, name), target = Path.Combine(stateRoot, name);
                if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
            }
            CopyData(Path.Combine(legacy, "data"), Path.Combine(stateRoot, "data"));
            RemapLegacyConfig(Path.Combine(stateRoot, "config.json"), Path.Combine(legacy, "data"), Path.Combine(stateRoot, "data"));
            File.WriteAllText(marker, "{\"source\":" + Json.Serialize(legacy) + "}\n", new UTF8Encoding(false));
            using (RegistryKey run = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
            {
                if (run.GetValue("QwenImageStudio") != null)
                {
                    run.SetValue("MLACStudio", "\"" + Path.Combine(installRoot, "MLACStudioBootstrap.exe") + "\" --launch --startup");
                    run.DeleteValue("QwenImageStudio", false);
                }
            }
        }

        private static void RemapLegacyConfig(string configPath, string legacyData, string currentData)
        {
            if (!File.Exists(configPath)) return;
            try
            {
                Dictionary<string, object> config = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(configPath, Encoding.UTF8));
                if (config == null || !config.ContainsKey("data_dir")) return;
                string configured = Convert.ToString(config["data_dir"]);
                if (String.Equals(Path.GetFullPath(configured), Path.GetFullPath(legacyData), StringComparison.OrdinalIgnoreCase))
                {
                    config["data_dir"] = Path.GetFullPath(currentData);
                    WriteAtomic(configPath, Json.Serialize(config));
                }
            }
            catch (Exception) { }
        }

        private static void CopyData(string source, string target)
        {
            if (!Directory.Exists(source)) return;
            foreach (string directory in Directory.GetDirectories(source, "*", SearchOption.AllDirectories))
            {
                string relative = directory.Substring(source.Length).TrimStart(Path.DirectorySeparatorChar);
                if (relative.Split(Path.DirectorySeparatorChar)[0].Equals("work", StringComparison.OrdinalIgnoreCase)) continue;
                Directory.CreateDirectory(Path.Combine(target, relative));
            }
            foreach (string file in Directory.GetFiles(source, "*", SearchOption.AllDirectories))
            {
                string relative = file.Substring(source.Length).TrimStart(Path.DirectorySeparatorChar);
                string lower = relative.ToLowerInvariant();
                if (relative.Split(Path.DirectorySeparatorChar)[0].Equals("work", StringComparison.OrdinalIgnoreCase) || lower.EndsWith(".gguf") || lower.EndsWith(".safetensors") || lower.EndsWith(".ckpt") || lower.EndsWith(".pt") || lower.EndsWith(".pth")) continue;
                string destination = Path.Combine(target, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                if (!File.Exists(destination)) File.Copy(file, destination);
            }
        }
    }
}
