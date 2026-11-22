# Shows, for every active Windows playback device: master volume/mute, and each app's
# volume, mute state and live sound level. Use it when "the bot is playing but I hear nothing":
# if Discord's level is above 0, Discord IS producing sound and the problem is the device.
#   powershell -ExecutionPolicy Bypass -File tools\audio-check.ps1

$code = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace AudioCheck {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {}

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
    }
    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection {
        int GetCount(out int count);
        int Item(int index, out IMMDevice device);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        int OpenPropertyStore(int access, out IntPtr store);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    }
    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        int RegisterControlChangeNotify(IntPtr n); int UnregisterControlChangeNotify(IntPtr n);
        int GetChannelCount(out int count);
        int SetMasterVolumeLevel(float level, ref Guid ctx); int SetMasterVolumeLevelScalar(float level, ref Guid ctx);
        int GetMasterVolumeLevel(out float level); int GetMasterVolumeLevelScalar(out float level);
        int SetChannelVolumeLevel(int ch, float level, ref Guid ctx); int SetChannelVolumeLevelScalar(int ch, float level, ref Guid ctx);
        int GetChannelVolumeLevel(int ch, out float level); int GetChannelVolumeLevelScalar(int ch, out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid ctx);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }
    [ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioMeterInformation { int GetPeakValue(out float peak); }

    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2 {
        int GetAudioSessionControl(IntPtr guid, int flags, out IntPtr control);
        int GetSimpleAudioVolume(IntPtr guid, int flags, out IntPtr volume);
        int GetSessionEnumerator(out IAudioSessionEnumerator sessions);
    }
    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator {
        int GetCount(out int count);
        int GetSession(int index, out IAudioSessionControl2 session);
    }
    [ComImport, Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2 {
        int GetState(out int state);
        int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        int SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string name, ref Guid ctx);
        int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string path);
        int SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid ctx);
        int GetGroupingParam(out Guid param); int SetGroupingParam(ref Guid param, ref Guid ctx);
        int RegisterAudioSessionNotification(IntPtr n); int UnregisterAudioSessionNotification(IntPtr n);
        int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        int GetProcessId(out uint pid);
    }
    [ComImport, Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface ISimpleAudioVolume {
        int SetMasterVolume(float level, ref Guid ctx); int GetMasterVolume(out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid ctx);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }

    public class Row {
        public string Device; public bool IsDefault; public float DeviceVolume; public bool DeviceMuted; public float DevicePeak;
        public uint Pid; public float Volume; public bool Muted; public float Peak; public int State;
    }

    public static class Probe {
        public static List<Row> Run(int samples, int delayMs) {
            var rows = new List<Row>();
            var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
            IMMDevice def; string defId = "";
            if (enumerator.GetDefaultAudioEndpoint(0, 1, out def) == 0) def.GetId(out defId);
            IMMDeviceCollection devices; enumerator.EnumAudioEndpoints(0, 1, out devices);
            int count; devices.GetCount(out count);
            for (int d = 0; d < count; d++) {
                IMMDevice device; devices.Item(d, out device);
                string id; device.GetId(out id);
                object o; Guid iid = typeof(IAudioEndpointVolume).GUID;
                device.Activate(ref iid, 23, IntPtr.Zero, out o);
                var endpoint = (IAudioEndpointVolume)o;
                float devVol; bool devMute; endpoint.GetMasterVolumeLevelScalar(out devVol); endpoint.GetMute(out devMute);
                iid = typeof(IAudioMeterInformation).GUID; device.Activate(ref iid, 23, IntPtr.Zero, out o);
                var devMeter = (IAudioMeterInformation)o;
                iid = typeof(IAudioSessionManager2).GUID; device.Activate(ref iid, 23, IntPtr.Zero, out o);
                var manager = (IAudioSessionManager2)o;
                IAudioSessionEnumerator sessions; manager.GetSessionEnumerator(out sessions);
                int n; sessions.GetCount(out n);
                var local = new List<Row>(); var meters = new List<IAudioMeterInformation>();
                for (int i = 0; i < n; i++) {
                    IAudioSessionControl2 session; sessions.GetSession(i, out session);
                    var row = new Row { Device = id, IsDefault = id == defId, DeviceVolume = devVol, DeviceMuted = devMute };
                    session.GetProcessId(out row.Pid); session.GetState(out row.State);
                    var vol = (ISimpleAudioVolume)session; vol.GetMasterVolume(out row.Volume); vol.GetMute(out row.Muted);
                    local.Add(row); meters.Add((IAudioMeterInformation)session);
                }
                float devPeak = 0;
                for (int s = 0; s < samples; s++) {
                    float p; devMeter.GetPeakValue(out p); if (p > devPeak) devPeak = p;
                    for (int i = 0; i < local.Count; i++) { meters[i].GetPeakValue(out p); if (p > local[i].Peak) local[i].Peak = p; }
                    System.Threading.Thread.Sleep(delayMs);
                }
                if (local.Count == 0) local.Add(new Row { Device = id, IsDefault = id == defId, DeviceVolume = devVol, DeviceMuted = devMute, Pid = 0, State = -1 });
                foreach (var r in local) { r.DevicePeak = devPeak; rows.Add(r); }
            }
            return rows;
        }
    }
}
'@
Add-Type -TypeDefinition $code -Language CSharp

function Get-DeviceName($id) {
    $guid = ($id -split '\.')[-1]
    $key = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\$guid\Properties"
    try {
        $p = Get-ItemProperty $key -ErrorAction Stop
        $desc = $p.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
        $iface = $p.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
        return "$desc ($iface)"
    } catch { return $id }
}

$rows = [AudioCheck.Probe]::Run(30, 100)      # sample sound levels for 3 seconds
$rows | Group-Object Device | ForEach-Object {
    $first = $_.Group[0]
    $tag = if ($first.IsDefault) { "  <== DEFAULT OUTPUT" } else { "" }
    "DEVICE: {0}{1}" -f (Get-DeviceName $first.Device), $tag
    "   master volume {0:P0}  muted: {1}  sound level right now: {2:P0}" -f $first.DeviceVolume, $first.DeviceMuted, $first.DevicePeak
    foreach ($r in $_.Group) {
        if ($r.State -lt 0) { continue }
        $name = if ($r.Pid -eq 0) { "System sounds" } else { try { (Get-Process -Id $r.Pid -ErrorAction Stop).ProcessName } catch { "pid $($r.Pid)" } }
        $state = @("inactive", "ACTIVE", "expired")[$r.State]
        "   app {0,-22} volume {1,4:P0}  muted: {2,-5}  level: {3,4:P0}  {4}" -f $name, $r.Volume, $r.Muted, $r.Peak, $state
    }
}
