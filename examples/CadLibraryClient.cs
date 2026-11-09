// Uses System.Net.Http; JSON parsing can use the plugin's existing JSON library.
using System;
using System.IO;
using System.Net.Http;
using System.Security.Cryptography;
using System.Threading.Tasks;

public sealed class CadLibraryClient : IDisposable
{
    private readonly HttpClient http = new HttpClient {
        BaseAddress = new Uri("http://127.0.0.1:8765"),
        Timeout = TimeSpan.FromMinutes(10)
    };

    public Task<string> SearchAsync(string query, string section, int limit = 5)
    {
        return http.GetStringAsync("/v1/search?q=" + Uri.EscapeDataString(query)
            + "&section=" + Uri.EscapeDataString(section) + "&limit=" + limit);
    }

    public Task<string> GetModelAsync(string modelId)
    {
        return http.GetStringAsync("/v1/models/" + Uri.EscapeDataString(modelId));
    }

    // destination must be a new file chosen by the caller. No CAD is auto-opened.
    public async Task DownloadAsync(string modelId, bool package, string destination, string expectedSha256)
    {
        if (String.IsNullOrWhiteSpace(expectedSha256) || expectedSha256.Length != 64)
            throw new ArgumentException("Use entry.sha256 or downloads.package_sha256 from the detail response.");
        var endpoint = "/v1/models/" + Uri.EscapeDataString(modelId) + (package ? "/package" : "/file");
        var temporary = destination + "." + Guid.NewGuid().ToString("N") + ".part";
        try {
            using (var response = await http.GetAsync(endpoint, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false)) {
                response.EnsureSuccessStatusCode();
                using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write))
                    await response.Content.CopyToAsync(output).ConfigureAwait(false);
            }
            string actual;
            using (var input = File.OpenRead(temporary))
            using (var sha = SHA256.Create())
                actual = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
            if (!String.Equals(actual, expectedSha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("CAD download SHA-256 mismatch");
            File.Move(temporary, destination);
        } finally {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
    }
    public void Dispose() { http.Dispose(); }
}
