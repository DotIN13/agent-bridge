const KEY = "ab-webui-token";

/**
 * The token the server printed, taken out of the URL fragment.
 *
 * A fragment is the one part of a URL a browser never sends to a server, so the
 * token stays out of access logs and out of the `Referer` on anything the page
 * loads. It is then moved into `sessionStorage` and stripped from the address
 * bar: a reload in this tab still works, and the URL that ends up pasted into a
 * chat window does not carry the credential.
 */
export function readToken(): string | null {
  const hash = window.location.hash.replace(/^#/, "");
  const fromUrl = new URLSearchParams(hash).get("t");
  if (fromUrl) {
    sessionStorage.setItem(KEY, fromUrl);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return fromUrl;
  }
  return sessionStorage.getItem(KEY);
}
