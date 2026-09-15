"""
Pre-flight environment check for kimodo-houdini-bridge.
Run with system Python (no venv needed): python scripts/check_env.py
"""

import os
import shutil
import socket
import subprocess
import sys

# API port (8001 by default; 8000 is reserved by Docker Desktop on Windows) + text-encoder.
REQUIRED_PORTS = [int(os.environ.get("KIMODO_PORT", "8001")), 9550]
RECOMMENDED_RAM_GB = 16


def check(label, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" \u2014 {detail}"
    print(line)
    return ok


def ram_gb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024**3)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
    except Exception:
        return None


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


def run(cmd):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=60
    )
    return result.returncode == 0, result.stdout + result.stderr


def main():
    results = []

    # Docker available
    ok = shutil.which("docker") is not None
    results.append(check("Docker CLI found", ok))

    if ok:
        # Docker daemon running
        daemon_ok, _ = run("docker info")
        results.append(check("Docker daemon running", daemon_ok))

        # GPU pass-through
        gpu_ok, out = run(
            "docker run --rm --gpus all ubuntu nvidia-smi"
        )
        results.append(
            check(
                "Docker GPU pass-through (nvidia-smi visible in container)",
                gpu_ok,
                "" if gpu_ok else "Check nvidia-container-toolkit and WSL2 GPU support",
            )
        )

    # System RAM
    gb = ram_gb()
    if gb is not None:
        ram_ok = gb >= RECOMMENDED_RAM_GB
        results.append(
            check(
                f"System RAM >= {RECOMMENDED_RAM_GB} GB",
                ram_ok,
                f"{gb:.1f} GB detected (text encoder runs on CPU and needs memory)",
            )
        )
    else:
        print("[SKIP] RAM check \u2014 could not read system memory")

    # Ports
    for port in REQUIRED_PORTS:
        free = port_free(port)
        results.append(
            check(
                f"Port {port} available",
                free,
                "" if free else f"Port {port} is already in use",
            )
        )

    print()
    passed = sum(results)
    total = len(results)
    print(f"Result: {passed}/{total} checks passed")
    if passed < total:
        print("Fix the FAIL items above before proceeding to Phase 1.")
        sys.exit(1)
    else:
        print("All checks passed. Ready for Phase 1.")


if __name__ == "__main__":
    main()
