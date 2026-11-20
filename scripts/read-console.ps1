# Writes the text on another process's console screen to a file.
#   -TargetPid <pid>   a process attached to the console to read (the hidden group-chat Claude)
#   -OutFile <path>    where the text goes (UTF-8)
#
# The bridge uses it when a started Claude never comes up: the CLI is then waiting at a
# startup question in its hidden console, and this shows which one. It must run as its own
# process, because a process can be attached to only one console at a time.
param([Parameter(Mandatory)][int]$TargetPid, [Parameter(Mandatory)][string]$OutFile)
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ConsoleScreen {
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool FreeConsole();
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool AttachConsole(uint pid);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr h);
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  static extern IntPtr CreateFile(string n, uint a, uint s, IntPtr sa, uint c, uint f, IntPtr t);
  [StructLayout(LayoutKind.Sequential)] struct COORD { public short X; public short Y; }
  [StructLayout(LayoutKind.Sequential)] struct RECT { public short L; public short T; public short R; public short B; }
  [StructLayout(LayoutKind.Sequential)] struct INFO { public COORD Size; public COORD Cur; public ushort Attr; public RECT Win; public COORD Max; }
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetConsoleScreenBufferInfo(IntPtr h, out INFO i);
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  static extern bool ReadConsoleOutputCharacter(IntPtr h, [Out] char[] buf, uint len, COORD at, out uint read);
  public static string Read(uint pid) {
    FreeConsole();
    if (!AttachConsole(pid)) return "";
    try {
      IntPtr h = CreateFile("CONOUT$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
      INFO i;
      if (h == new IntPtr(-1) || !GetConsoleScreenBufferInfo(h, out i)) return "";
      StringBuilder sb = new StringBuilder();
      for (short y = 0; y < i.Size.Y; y++) {
        char[] b = new char[i.Size.X]; uint r; COORD c; c.X = 0; c.Y = y;
        if (!ReadConsoleOutputCharacter(h, b, (uint)i.Size.X, c, out r)) continue;
        string s = new string(b, 0, (int)r).TrimEnd();
        if (s.Length > 0) sb.AppendLine(s);
      }
      CloseHandle(h);
      return sb.ToString();
    } finally { FreeConsole(); }
  }
}
'@
[IO.File]::WriteAllText($OutFile, [ConsoleScreen]::Read([uint32]$TargetPid), (New-Object Text.UTF8Encoding($false)))
