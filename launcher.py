import os, sys, json, uuid, shutil, threading, urllib.request, subprocess, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
import tkinter as tk
from tkinter import ttk
import logging

# ---------------------------
# Thư mục AppData
# ---------------------------
APPDATA_DIR = os.path.join(os.environ.get("APPDATA"), "Abcsnoob", "Minecraft_Launcher")
MC_DIR = os.path.join(APPDATA_DIR, "minecraft")
VERSIONS_DIR = os.path.join(MC_DIR, "versions")
LIBRARIES_DIR = os.path.join(MC_DIR, "libraries")
ASSETS_DIR = os.path.join(MC_DIR, "assets")
NATIVES_DIR = os.path.join(MC_DIR, "natives")
os.makedirs(MC_DIR, exist_ok=True)

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest.json"
THREADS = 25
LOG_PATH = os.path.join(APPDATA_DIR, "launcher.log")
os.makedirs(APPDATA_DIR, exist_ok=True)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------
# Minecraft Launcher Core
# ---------------------------
class MinecraftLauncher:
    def __init__(self, log_callback=None, progress_callback=None, file_callback=None):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.file_callback = file_callback
        self.downloaded_files = 0
        self.total_files = 0

        for d in [MC_DIR, VERSIONS_DIR, LIBRARIES_DIR, ASSETS_DIR, NATIVES_DIR]:
            os.makedirs(d, exist_ok=True)

        self.versions = {}
        self.load_versions()

    def log(self, msg):
        logging.info(msg)
        if self.log_callback:
            self.log_callback(msg)

    def progress(self, done, total):
        if self.progress_callback:
            self.progress_callback(done, total)

    def file_update(self, filename):
        if self.file_callback:
            self.file_callback(filename)

    def download_file(self, url, path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                self.downloaded_files += 1
                self.progress(self.downloaded_files, self.total_files)
                return
            self.file_update(os.path.basename(path))
            with urllib.request.urlopen(url, timeout=15) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            self.downloaded_files += 1
            self.progress(self.downloaded_files, self.total_files)
            self.log(f"[DONE] {path}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.log(f"[SKIP] Không tồn tại: {url}")
                self.downloaded_files += 1
                self.progress(self.downloaded_files, self.total_files)
            else:
                self.log(f"[ERROR] Lỗi tải {url}: {e}")
        except Exception as e:
            self.log(f"[ERROR] Lỗi tải {url}: {e}")

    def download_queue(self, tasks):
        self.total_files = len(tasks)
        self.downloaded_files = 0
        with ThreadPoolExecutor(max_workers=THREADS) as executor:
            futures = [executor.submit(self.download_file, url, path) for url, path in tasks]
            for _ in as_completed(futures):
                pass

    def load_versions(self):
        path = os.path.join(MC_DIR, "version_manifest.json")
        if not os.path.exists(path):
            self.download_file(MANIFEST_URL, path)
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.versions = {v["id"]: v for v in manifest["versions"]}

    def ensure_version(self, vid):
        vdir = os.path.join(VERSIONS_DIR, vid)
        vjson = os.path.join(vdir, f"{vid}.json")
        if not os.path.exists(vjson):
            meta = self.versions[vid]
            self.download_file(meta["url"], vjson)
        with open(vjson, "r", encoding="utf-8") as f:
            return json.load(f)

    def extract_natives(self, jar, natives_dir):
        with zipfile.ZipFile(jar, "r") as z:
            for n in z.namelist():
                if any(n.endswith(e) for e in [".dll", ".so", ".dylib"]):
                    z.extract(n, natives_dir)

    def find_java(self):
        candidates = []
        if "JAVA_HOME" in os.environ:
            candidates.append(os.path.join(os.environ["JAVA_HOME"], "bin", "java.exe"))
        candidates += [
            r"C:\Program Files\Eclipse Adoptium\jdk-21\bin\java.exe",
            r"C:\Program Files\Java\jdk-21\bin\java.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return "java"

    def ensure_libraries(self, vdata):
        cp = []
        tasks = []
        libs = vdata.get("libraries", [])

        # client jar
        client_url = vdata["downloads"]["client"]["url"]
        client_path = os.path.join(VERSIONS_DIR, vdata["id"], f"{vdata['id']}.jar")
        tasks.append((client_url, client_path))
        cp.append(client_path)

        # libraries
        for lib in libs:
            if "downloads" in lib and "artifact" in lib["downloads"]:
                path = os.path.join(LIBRARIES_DIR, lib["downloads"]["artifact"]["path"])
                url = lib["downloads"]["artifact"]["url"]
                tasks.append((url, path))
                cp.append(path)

        self.download_queue(tasks)
        return cp, client_path

    def ensure_assets(self, vdata):
        index_info = vdata.get("assetIndex")
        if not index_info:
            self.log("[INFO] Không có assetIndex")
            return

        if isinstance(index_info, dict):
            url = index_info.get("url")
        else:
            url = f"https://piston-meta.mojang.com/v1/packages/{index_info}/{index_info}.json"

        index_id = index_info.get("id") if isinstance(index_info, dict) else str(index_info)
        index_file = os.path.join(ASSETS_DIR, "indexes", f"{index_id}.json")
        if not os.path.exists(index_file):
            self.download_file(url, index_file)

        with open(index_file, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        objects = index_data.get("objects", {})
        tasks = []
        for name, info in objects.items():
            sha = info["hash"]
            path = os.path.join(ASSETS_DIR, "objects", sha[:2], sha)
            file_url = f"https://resources.download.minecraft.net/{sha[:2]}/{sha}"
            tasks.append((file_url, path))

        self.download_queue(tasks)

    def launch(self, vid, username="Player", offline=True):
        vdata = self.ensure_version(vid)
        cp, client_jar = self.ensure_libraries(vdata)

        if os.path.exists(NATIVES_DIR):
            shutil.rmtree(NATIVES_DIR)
        os.makedirs(NATIVES_DIR, exist_ok=True)

        for lib in vdata.get("libraries", []):
            if "downloads" in lib and "classifiers" in lib["downloads"]:
                for k, v in lib["downloads"]["classifiers"].items():
                    if "natives-windows" in k:
                        jarpath = os.path.join(LIBRARIES_DIR, v["path"])
                        if not os.path.exists(jarpath):
                            self.download_file(v["url"], jarpath)
                        self.extract_natives(jarpath, NATIVES_DIR)

        self.ensure_assets(vdata)
        java = self.find_java()
        args = [
            java,
            f"-Djava.library.path={NATIVES_DIR}",
            "-cp", ";".join(cp),
            vdata["mainClass"],
            "--username", username,
            "--version", vid,
            "--gameDir", MC_DIR,
            "--assetsDir", ASSETS_DIR,
            "--assetIndex", vdata["assets"] if isinstance(vdata["assets"], str) else vdata["assets"]["id"],
            "--uuid", str(uuid.uuid4()) if offline else "real-uuid",
            "--accessToken", "0" if offline else "real-token",
            "--userType", "mojang"
        ]
        self.log("Chạy Minecraft...")
        subprocess.Popen(args, cwd=MC_DIR)

# ---------------------------
# GUI
# ---------------------------
class LauncherGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Minecraft Launcher")
        self.log_box = tk.Text(root, height=15)
        self.log_box.pack()
        self.progress = ttk.Progressbar(root, length=400)
        self.progress.pack()
        self.file_label = tk.Label(root, text="Đang tải: None")
        self.file_label.pack()

        self.version_var = tk.StringVar()
        self.name_var = tk.StringVar(value="Player")
        tk.Label(root, text="Username").pack()
        tk.Entry(root, textvariable=self.name_var).pack()
        tk.Label(root, text="Version").pack()
        self.version_combo = ttk.Combobox(root, textvariable=self.version_var)
        self.version_combo.pack()
        tk.Button(root, text="Launch Offline", command=self.launch_offline).pack()

        self.launcher = MinecraftLauncher(
            log_callback=self.add_log,
            progress_callback=self.update_progress,
            file_callback=self.update_file
        )
        self.version_combo["values"] = list(self.launcher.versions.keys())
        if self.launcher.versions:
            self.version_var.set(list(self.launcher.versions.keys())[0])

    def add_log(self, msg):
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")

    def update_progress(self, done, total):
        self.progress["maximum"] = total
        self.progress["value"] = done

    def update_file(self, filename):
        self.file_label.config(text=f"Đang tải: {filename}")

    def launch_offline(self):
        threading.Thread(target=self._launch_thread).start()

    def _launch_thread(self):
        vid = self.version_var.get()
        username = self.name_var.get()
        self.launcher.launch(vid, username=username, offline=True)

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    root = tk.Tk()
    gui = LauncherGUI(root)
    root.mainloop()
