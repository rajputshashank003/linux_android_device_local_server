"""Bounded Docker Hub manifest preflight for the local PRoot image runner.

PRoot-Distro remains responsible for pulling and extracting images. This module
only rejects known-incompatible public Hub platforms before they consume disk.
Other registries are left to PRoot, avoiding arbitrary registry lookups/SSRF.
"""
from __future__ import annotations
import json, re, urllib.error, urllib.parse, urllib.request

_ACCEPT = ', '.join(('application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json'))
_MAX_JSON = 2 * 1024 * 1024

class RegistryUnavailable(RuntimeError):
    """A bounded public registry lookup could not be completed."""

class UnsupportedImage(RuntimeError):
    """Registry metadata proves local execution is unsupported."""

def _hub_repository(reference: str):
    if not isinstance(reference, str) or not reference or len(reference) > 256: return None
    name, sep, digest = reference.partition('@')
    if sep: selector = digest
    else:
        slash, colon = name.rfind('/'), name.rfind(':')
        if colon > slash: name, tag = name[:colon], name[colon + 1:]
        else: tag = 'latest'
        selector = tag
    parts = name.split('/', 1)
    if len(parts) == 2 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        if parts[0].lower() not in ('docker.io', 'index.docker.io', 'registry-1.docker.io'): return None
        name = parts[1]
    if not name or name.startswith('/') or name.endswith('/') or '//' in name: return None
    if '/' not in name: name = 'library/' + name
    if not re.fullmatch(r'[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*', name): return None
    if sep and not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', selector): return None
    if not sep and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', selector): return None
    return name, selector

def _read_json(url, headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=6.0) as response:
            raw, response_headers = response.read(_MAX_JSON + 1), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        if exc.code == 401: raise PermissionError(response_headers.get('www-authenticate', '')) from exc
        if exc.code == 404: raise UnsupportedImage('Docker Hub could not find the requested image or tag.') from exc
        if exc.code in (403, 429): raise RegistryUnavailable(f'Docker Hub rejected or rate-limited the manifest lookup (HTTP {exc.code}).') from exc
        raise RegistryUnavailable(f'Docker Hub returned HTTP {exc.code}.') from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RegistryUnavailable(f'Docker Hub manifest lookup failed ({type(exc).__name__}).') from exc
    if len(raw) > _MAX_JSON: raise RegistryUnavailable('Docker Hub metadata exceeded the 2 MiB safety limit.')
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise RegistryUnavailable('Docker Hub returned invalid image metadata.') from exc
    if not isinstance(value, dict): raise RegistryUnavailable('Docker Hub returned an unsupported manifest shape.')
    return value, response_headers

def _bearer_token(challenge, repository):
    match = re.fullmatch(r'Bearer\s+(.+)', challenge, flags=re.IGNORECASE)
    if not match: raise UnsupportedImage('This Docker Hub image requires registry credentials; the current Host form supports public images only.')
    params = dict(re.findall(r'([A-Za-z_]+)="([^"]*)"', match.group(1)))
    if params.get('realm') != 'https://auth.docker.io/token' or params.get('service') != 'registry.docker.io' or params.get('scope') not in (None, f'repository:{repository}:pull'):
        raise UnsupportedImage('Docker Hub requested an unsupported or unsafe registry authentication flow.')
    query = urllib.parse.urlencode({'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'})
    token_data, _ = _read_json('https://auth.docker.io/token?' + query, {'Accept':'application/json','User-Agent':'MyFile-Hosting/1'})
    token = token_data.get('token') or token_data.get('access_token')
    if not isinstance(token, str) or not token or len(token) > 8192: raise UnsupportedImage('Docker Hub did not provide a usable pull token.')
    return token

def inspect_public_docker_hub_image(reference: str, host_arch: str = 'aarch64'):
    """Return public image platform metadata, None for non-Hub images.

    Transient network failures remain distinguishable from confirmed platform
    incompatibility so the caller can defer to PRoot's native pull diagnostics.
    """
    resolved = _hub_repository(reference)
    if resolved is None: return None
    repository, selector = resolved
    quoted_repo = '/'.join(urllib.parse.quote(p, safe='._-') for p in repository.split('/'))
    quoted_selector = urllib.parse.quote(selector, safe=':')
    url = f'https://registry-1.docker.io/v2/{quoted_repo}/manifests/{quoted_selector}'
    headers = {'Accept':_ACCEPT,'User-Agent':'MyFile-Hosting/1'}
    try: manifest, response_headers = _read_json(url, headers)
    except PermissionError as exc:
        headers['Authorization'] = 'Bearer ' + _bearer_token(str(exc), repository)
        try: manifest, response_headers = _read_json(url, headers)
        except PermissionError as denied: raise UnsupportedImage('Docker Hub denied access; the image is private or unavailable to anonymous pulls.') from denied
    media_type = manifest.get('mediaType') or response_headers.get('content-type', '').split(';', 1)[0]
    host = host_arch.lower()
    wanted = {'aarch64':'arm64','arm64':'arm64','x86_64':'amd64','amd64':'amd64','armv7l':'arm'}.get(host, host)
    descriptors = manifest.get('manifests')
    if isinstance(descriptors, list):
        platforms, compatible = [], []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict): continue
            platform = descriptor.get('platform') or {}
            if not isinstance(platform, dict): continue
            item = {'os':platform.get('os'),'architecture':platform.get('architecture'),'variant':platform.get('variant')}
            if item['os'] and item['architecture']: platforms.append(item)
            if item['os'] == 'linux' and item['architecture'] == wanted: compatible.append(item)
        if not compatible:
            available = ', '.join(sorted({f"{p['os']}/{p['architecture']}" + (f"/{p['variant']}" if p.get('variant') else '') for p in platforms})) or 'no platform descriptors'
            raise UnsupportedImage(f'Docker Hub image has no linux/{wanted} manifest for this device. Published platforms: {available}. Use a compatible image or configure a remote Docker Engine.')
        return {'registry':'docker.io','repository':repository,'selector':selector,'architecture':wanted,'platforms':platforms,'manifestType':media_type or 'index'}
    config = manifest.get('config')
    digest = str(config.get('digest', '')) if isinstance(config, dict) else ''
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest): raise RegistryUnavailable('Docker Hub manifest has no supported image config descriptor.')
    try: image_config, _ = _read_json(f'https://registry-1.docker.io/v2/{quoted_repo}/blobs/{digest}', headers)
    except PermissionError as exc:
        headers['Authorization'] = 'Bearer ' + _bearer_token(str(exc), repository)
        try: image_config, _ = _read_json(f'https://registry-1.docker.io/v2/{quoted_repo}/blobs/{digest}', headers)
        except PermissionError as denied: raise UnsupportedImage('Docker Hub denied access to the image config; it is private or unavailable to anonymous pulls.') from denied
    image_arch, image_os = str(image_config.get('architecture','')).lower(), str(image_config.get('os','linux')).lower()
    if image_os != 'linux' or image_arch != wanted:
        raise UnsupportedImage(f'Docker Hub image resolves to {image_os}/{image_arch or "unknown"}, but this device requires linux/{wanted}. Use a compatible image or configure a remote Docker Engine.')
    return {'registry':'docker.io','repository':repository,'selector':selector,'architecture':image_arch,'platforms':[{'os':image_os,'architecture':image_arch}],'manifestType':media_type or 'image manifest'}
