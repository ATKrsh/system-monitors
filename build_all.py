import os
import subprocess
import shutil

apps = ["cpuload", "cputemp", "gputemp", "gpuusage", "hddled", "netled", "powerusage", "ramusage", "unified"]

# Make sure top-level dist exists
os.makedirs("dist", exist_ok=True)

for app in apps:
    print(f"\n========================================\nBuilding: {app}\n========================================")
    spec_file = f"{app}.spec"
    try:
        # Run pyinstaller inside the application's subdirectory
        subprocess.run(["pyinstaller", spec_file], cwd=app, check=True)
        
        # Copy the compiled executable to the top-level dist directory
        src_exe = os.path.join(app, "dist", f"{app}.exe")
        dest_exe = os.path.join("dist", f"{app}.exe")
        if os.path.exists(src_exe):
            shutil.copy2(src_exe, dest_exe)
            print(f"Copied {src_exe} -> {dest_exe}")
        else:
            print(f"Error: Compiled executable {src_exe} not found!")
    except subprocess.CalledProcessError as e:
        print(f"PyInstaller failed for {app} with exit code {e.returncode}")
    except Exception as e:
        print(f"An error occurred while processing {app}: {e}")

print("\nAll builds completed.")
