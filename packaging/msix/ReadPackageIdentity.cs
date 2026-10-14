// Read-only Windows Appx identity binding for release verification.
// Interface order and identifiers follow Windows SDK AppxPackaging.h.
// This helper reads the manifest through Windows; it never registers a package.
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Collections.Generic;
using System.Text;

namespace Ofca.Msix
{
    public sealed class PackageIdentity
    {
        public string Name;
        public string Publisher;
        public string Version;
        public string Architecture;
        public string FullName;
        public string FamilyName;
    }

    [ComImport, Guid("beb94909-e451-438b-b5a7-d79e767b75d8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAppxFactory
    {
        void CreatePackageWriter(IntPtr stream, IntPtr settings, out IntPtr writer);
        void CreatePackageReader(IntPtr stream, out IntPtr reader);
        void CreateManifestReader([MarshalAs(UnmanagedType.Interface)] IStream stream, out IAppxManifestReader reader);
    }

    [ComImport, Guid("4e1bd148-55a0-4480-a3d1-15544710637c"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAppxManifestReader
    {
        void GetPackageId(out IAppxManifestPackageId identity);
    }

    [ComImport, Guid("283ce2d7-7153-4a91-9649-7a0f7240945f"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAppxManifestPackageId
    {
        void GetName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        void GetArchitecture(out int architecture);
        void GetPublisher([MarshalAs(UnmanagedType.LPWStr)] out string publisher);
        void GetVersion(out ulong version);
        void GetResourceId([MarshalAs(UnmanagedType.LPWStr)] out string resourceId);
        void ComparePublisher([MarshalAs(UnmanagedType.LPWStr)] string other, [MarshalAs(UnmanagedType.Bool)] out bool same);
        void GetPackageFullName([MarshalAs(UnmanagedType.LPWStr)] out string fullName);
        void GetPackageFamilyName([MarshalAs(UnmanagedType.LPWStr)] out string familyName);
    }

    public static class IdentityReader
    {
        public static string DecodePartName(string part)
        {
            var result = new StringBuilder();
            var utf8 = new UTF8Encoding(false, true);
            for (int index = 0; index < part.Length;)
            {
                if (part[index] != '%') { result.Append(part[index++]); continue; }
                var bytes = new List<byte>();
                while (index < part.Length && part[index] == '%')
                {
                    if (index + 2 >= part.Length) throw new ArgumentException("Invalid MSIX part escape");
                    bytes.Add(Convert.ToByte(part.Substring(index + 1, 2), 16));
                    index += 3;
                }
                result.Append(utf8.GetString(bytes.ToArray()));
            }
            return result.ToString();
        }

        [DllImport("shlwapi.dll", ExactSpelling = true)]
        private static extern IntPtr SHCreateMemStream(byte[] bytes, uint length);

        public static PackageIdentity Read(byte[] manifest)
        {
            if (manifest == null || manifest.Length == 0 || manifest.Length > 1024 * 1024)
                throw new ArgumentException("Manifest exceeds its bound");
            IntPtr pointer = SHCreateMemStream(manifest, (uint)manifest.Length);
            if (pointer == IntPtr.Zero) throw new OutOfMemoryException();
            object stream = null;
            object factory = null;
            IAppxManifestReader reader = null;
            IAppxManifestPackageId identity = null;
            try
            {
                stream = Marshal.GetObjectForIUnknown(pointer);
                Marshal.Release(pointer);
                pointer = IntPtr.Zero;
                factory = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("5842a140-ff9f-4166-8f5c-62f5b7b0c781"), true));
                ((IAppxFactory)factory).CreateManifestReader((IStream)stream, out reader);
                reader.GetPackageId(out identity);
                string name, publisher, fullName, familyName;
                int architecture;
                ulong version;
                identity.GetName(out name);
                identity.GetPublisher(out publisher);
                identity.GetArchitecture(out architecture);
                identity.GetVersion(out version);
                identity.GetPackageFullName(out fullName);
                identity.GetPackageFamilyName(out familyName);
                if (architecture != 9) throw new ArgumentException("Only x64 packages are qualified");
                return new PackageIdentity {
                    Name = name, Publisher = publisher, Architecture = "x64",
                    Version = String.Format("{0}.{1}.{2}.{3}", version >> 48, (version >> 32) & 65535, (version >> 16) & 65535, version & 65535),
                    FullName = fullName, FamilyName = familyName
                };
            }
            finally
            {
                if (identity != null) Marshal.ReleaseComObject(identity);
                if (reader != null) Marshal.ReleaseComObject(reader);
                if (factory != null) Marshal.ReleaseComObject(factory);
                if (stream != null) Marshal.ReleaseComObject(stream);
                if (pointer != IntPtr.Zero) Marshal.Release(pointer);
            }
        }
    }
}
