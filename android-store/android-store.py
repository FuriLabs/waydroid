#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, signal
from dbus_next import BusType, Variant
import asyncio
import os
import aiohttp
import json
from pathlib import Path

REPO_CONFIG_DIR = "/etc/android-store/repos"
CACHE_DIR = os.path.expanduser("~/.cache/android-store/repo")
DOWNLOAD_CACHE_DIR = os.path.expanduser("~/.cache/android-store/downloads")

class FDroidInterface(ServiceInterface):
    def __init__(self):
        super().__init__('io.FuriOS.AndroidStore.fdroid')
        self.session = None

    async def ensure_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def cleanup_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    def read_repo_list(self, repo_file):
        try:
            with open(os.path.join(REPO_CONFIG_DIR, repo_file), 'r') as f:
                return [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except FileNotFoundError:
            return []

    async def download_index(self, repo_url, repo_name):
        await self.ensure_session()

        repo_cache_dir = os.path.join(CACHE_DIR, repo_name)
        os.makedirs(repo_cache_dir, exist_ok=True)

        index_url = f"{repo_url.rstrip('/')}/index-v2.json"
        try:
            async with self.session.get(index_url) as response:
                if response.status == 200:
                    json_content = await response.text()
                    index_path = os.path.join(repo_cache_dir, 'index-v2.json')
                    with open(index_path, 'w') as f:
                        f.write(json_content)
                    return True
            return False
        except Exception as e:
            print(f"Error downloading index for {repo_url}: {e}")
            return False

    def get_localized_text(self, text_obj, lang='en-US'):
        if isinstance(text_obj, dict):
            return text_obj.get(lang, list(text_obj.values())[0] if text_obj else 'N/A')
        return text_obj if text_obj else 'N/A'

    def get_latest_version(self, versions):
        if not versions:
            return None

        latest = sorted(
            versions.items(),
            key=lambda x: x[1]['manifest']['versionCode'] if 'versionCode' in x[1]['manifest'] else 0,
            reverse=True
        )[0]
        return latest[1]

    def get_package_info(self, package_id, metadata, version_info, repo_url):
        apk_name = version_info['file']['name']
        download_url = f"{repo_url.rstrip('/')}{apk_name}"

        icon_url = 'N/A'
        if 'icon' in metadata:
            icon_path = self.get_localized_text(metadata['icon'])
            if isinstance(icon_path, dict) and 'name' in icon_path:
                icon_url = f"{repo_url.rstrip('/')}{icon_path['name']}"

        manifest = version_info['manifest']
        return {
            'apk_name': apk_name.lstrip('/'),
            'download_url': download_url,
            'icon_url': icon_url,
            'version': manifest.get('versionName', 'N/A'),
            'version_code': manifest.get('versionCode', 'N/A'),
            'size': version_info['file'].get('size', 'N/A'),
            'min_sdk': manifest.get('usesSdk', {}).get('minSdkVersion', 'N/A'),
            'target_sdk': manifest.get('usesSdk', {}).get('targetSdkVersion', 'N/A'),
            'permissions': [p['name'] for p in manifest.get('usesPermission', []) if isinstance(p, dict)],
            'features': manifest.get('features', []),
            'hash': version_info['file'].get('sha256', 'N/A'),
            'hash_type': 'sha256'
        }

    async def install_app(self, package_path):
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()

            introspection = await bus.introspect('id.waydro.Session', '/SessionManager')
            proxy = bus.get_proxy_object('id.waydro.Session', '/SessionManager', introspection)
            interface = proxy.get_interface('id.waydro.SessionManager')

            await interface.call_install_app(package_path)

            bus.disconnect()
            return True
        except Exception as e:
            print(f"Error installing app: {e}")
            return False

    async def remove_app(self, package_name):
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()

            introspection = await bus.introspect('id.waydro.Session', '/SessionManager')
            proxy = bus.get_proxy_object('id.waydro.Session', '/SessionManager', introspection)
            interface = proxy.get_interface('id.waydro.SessionManager')

            await interface.call_remove_app(package_name)

            bus.disconnect()
            return True
        except Exception as e:
            print(f"Error removing app: {e}")
            return False

    async def get_apps_info(self):
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            introspection = await bus.introspect('id.waydro.Session', '/SessionManager')
            proxy = bus.get_proxy_object('id.waydro.Session', '/SessionManager', introspection)
            interface = proxy.get_interface('id.waydro.SessionManager')

            apps_info = await interface.call_get_apps_info()
            result = []

            for app in apps_info:
                app_info = {
                    'id': Variant('s', app['packageName'].value),
                    'packageName': Variant('s', app['packageName'].value),
                    'name': Variant('s', app['name'].value),
                    'versionName': Variant('s', app['versionName'].value),
                    'state': Variant('s', 'installed')
                }
                result.append(app_info)

            bus.disconnect()
            return result
        except Exception as e:
            print(f"Error getting apps info: {e}")
            return []

    @method()
    async def Search(self, query: 's') -> 's':
        print(f"Searching for {query}")
        results = []
        if not os.path.exists(CACHE_DIR):
            print("Cache directory not found. Please run UpdateCache first.")
            return json.dumps(results)

        for repo_dir in os.listdir(CACHE_DIR):
            index_path = os.path.join(CACHE_DIR, repo_dir, 'index-v2.json')
            if not os.path.exists(index_path):
                continue

            try:
                repo_url = None
                with open(os.path.join(REPO_CONFIG_DIR, repo_dir), 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            repo_url = line
                            break

                if not repo_url:
                    continue

                with open(index_path, 'r') as f:
                    index_data = json.load(f)

                for package_id, package_data in index_data['packages'].items():
                    name = self.get_localized_text(package_data['metadata'].get('name', ''))
                    if query.lower() in name.lower():
                        latest_version = self.get_latest_version(package_data['versions'])
                        if latest_version:
                            package_info = self.get_package_info(package_id, package_data['metadata'], latest_version, repo_url)
                            metadata = package_data['metadata']

                            app_info = {
                                'repository': repo_dir,
                                'id': package_id,
                                'name': name,
                                'summary': self.get_localized_text(metadata.get('summary', 'N/A')),
                                'description': self.get_localized_text(metadata.get('description', 'N/A')),
                                'license': metadata.get('license', 'N/A'),
                                'categories': metadata.get('categories', []),
                                'author': metadata.get('author', {}).get('name', 'N/A'),
                                'web_url': metadata.get('webSite', 'N/A'),
                                'source_url': metadata.get('sourceCode', 'N/A'),
                                'tracker_url': metadata.get('issueTracker', 'N/A'),
                                'changelog_url': metadata.get('changelog', 'N/A'),
                                'donation_url': metadata.get('donate', 'N/A'),
                                'added_date': metadata.get('added', 'N/A'),
                                'last_updated': metadata.get('lastUpdated', 'N/A'),
                                'package': package_info
                            }
                            results.append(app_info)
            except Exception as e:
                print(f"Error parsing {index_path}: {e}")
                continue

        return json.dumps(results)

    @method()
    async def UpdateCache(self) -> 'b':
        success = True
        processed_repos = set()

        os.makedirs(CACHE_DIR, exist_ok=True)

        for config_file in os.listdir(REPO_CONFIG_DIR):
            if not os.path.isfile(os.path.join(REPO_CONFIG_DIR, config_file)):
                continue

            # skip if we've already successfully processed this repo (don't redownload from a mirror)
            if config_file in processed_repos:
                continue

            repos = self.read_repo_list(config_file)
            repo_success = False

            for repo_url in repos:
                repo_name = config_file
                print(f"Downloading {repo_name} index from {repo_url}")

                if await self.download_index(repo_url, repo_name):
                    print(f"Successfully downloaded {repo_name}")
                    repo_success = True
                    processed_repos.add(config_file)
                    break
                else:
                    print(f"Failed to download from {repo_url}, trying next mirror...")

            if not repo_success:
                print(f"Failed to download {repo_name} from all mirrors")
                success = False

        await self.cleanup_session()
        return success

    @method()
    async def Install(self, package_id: 's') -> 'b':
        print(f"Install {package_id}")
        if not os.path.exists(CACHE_DIR):
            print("Cache directory not found. Please run UpdateCache first.")
            return False

        try:
            package_info = None
            repo_url = None

            for repo_dir in os.listdir(CACHE_DIR):
                index_path = os.path.join(CACHE_DIR, repo_dir, 'index-v2.json')
                if not os.path.exists(index_path):
                    continue

                with open(os.path.join(REPO_CONFIG_DIR, repo_dir), 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            repo_url = line
                            break

                if not repo_url:
                    continue

                with open(index_path, 'r') as f:
                    index_data = json.load(f)

                if package_id in index_data['packages']:
                    package_data = index_data['packages'][package_id]
                    latest_version = self.get_latest_version(package_data['versions'])
                    if latest_version:
                        package_info = self.get_package_info(package_id, package_data['metadata'], latest_version, repo_url)
                        break

            if not package_info:
                print(f"Package {package_id} not found")
                return False

            os.makedirs(DOWNLOAD_CACHE_DIR, exist_ok=True)

            await self.ensure_session()
            filepath = os.path.join(DOWNLOAD_CACHE_DIR, package_info['apk_name'])

            async with self.session.get(package_info['download_url']) as response:
                if response.status == 200:
                    with open(filepath, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    print(f"APK downloaded to: {filepath}")

                    success = await self.install_app(filepath)

                    os.remove(filepath)

                    if success:
                        self.AppInstalled(package_id)
                        print(f"Successfully installed {package_id}")
                        return True
                    else:
                        print(f"Failed to install {package_id}")
                        return False
                else:
                    print(f"Download failed with status: {response.status}")
                    return False
        except Exception as e:
            print(f"Installation failed: {e}")
            return False

    @signal()
    def AppInstalled(self, package_id: 's') -> 's':
        return package_id

    @method()
    def GetRepositories(self) -> 'a(ss)':
        print("GetRepositories")
        repositories = []

        try:
            for repo_file in os.listdir(REPO_CONFIG_DIR):
                repo_path = os.path.join(REPO_CONFIG_DIR, repo_file)
                if not os.path.isfile(repo_path):
                    continue

                with open(repo_path, 'r') as f:
                    lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                    if lines:
                        repositories.append([repo_file, lines[0]])
            return repositories
        except Exception as e:
            print(f"Error reading repositories: {e}")
            return []

    @method()
    async def GetUpgradable(self) -> 'aa{sv}':
        print("GetUpgradable")
        upgradable = []
        raw_upgradable = await self.get_upgradable_packages()

        for pkg in raw_upgradable:
            upgradable_info = {
                'id': Variant('s', pkg['id']),
                'name': Variant('s', pkg.get('name', pkg['id'])),
                'packageName': Variant('s', pkg['id']),
                'currentVersion': Variant('s', pkg['current_version']),
                'availableVersion': Variant('s', pkg['available_version']),
                'repository': Variant('s', pkg['repo_url']),
                'package': Variant('s', json.dumps(pkg['packageInfo']))
            }
            print(f"{upgradable_info['packageName'].value} {upgradable_info['name'].value} {upgradable_info['currentVersion'].value} {upgradable_info['availableVersion'].value}")
            upgradable.append(upgradable_info)
        return upgradable

    async def get_upgradable_packages(self):
        upgradable = []
        installed_apps = await self.get_apps_info()

        if not os.path.exists(CACHE_DIR):
            print("Cache directory not found. Please run UpdateCache first.")
            return []

        for app in installed_apps:
            package_name = app['packageName'].value
            current_version = app['versionName'].value

            for repo_dir in os.listdir(CACHE_DIR):
                index_path = os.path.join(CACHE_DIR, repo_dir, 'index-v2.json')
                if not os.path.exists(index_path):
                    continue

                try:
                    repo_url = None
                    with open(os.path.join(REPO_CONFIG_DIR, repo_dir), 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                repo_url = line
                                break

                    if not repo_url:
                        continue

                    with open(index_path, 'r') as f:
                        index_data = json.load(f)

                    if package_name in index_data['packages']:
                        package_data = index_data['packages'][package_name]
                        latest_version = self.get_latest_version(package_data['versions'])
                        if latest_version:
                            repo_version = latest_version['manifest']['versionName']
                            if repo_version != current_version:
                                package_info = self.get_package_info(
                                    package_name,
                                    package_data['metadata'],
                                    latest_version,
                                    repo_url
                                )
                                upgradable_info = {
                                    'id': package_name,
                                    'packageInfo': package_info,
                                    'repo_url': repo_url,
                                    'current_version': current_version,
                                    'available_version': repo_version,
                                    'name': self.get_localized_text(package_data['metadata'].get('name', package_name))
                                }
                                upgradable.append(upgradable_info)
                                break
                except Exception as e:
                    print(f"Error parsing {index_path}: {e}")
                    continue
        return upgradable

    @method()
    async def UpgradePackages(self, packages: 'as') -> 'b':
        print(f"UpgradePackages: {packages}")
        upgradable = await self.get_upgradable_packages()

        for package in packages:
            for pkg in upgradable:
                if pkg['id'] == package:
                    print(f"Installing upgrade for {package}")
                    success = await self.install_app(pkg['packageInfo']['download_url'])
                    if not success:
                        print(f"Failed to upgrade {package}")
                        return False
        return True

    @method()
    def RemoveRepository(self, repo_id: 's') -> 'b':
        print(f"RemoveRepository: {repo_id}")
        return True

    @method()
    async def GetInstalledApps(self) -> 'aa{sv}':
        print("GetInstalledApps")
        return await self.get_apps_info()

    @method()
    async def UninstallApp(self, package_name: 's') -> 'b':
        print(f"UninstallApp: {package_name}")
        return await self.remove_app(package_name)

class AndroidStoreService:
    def __init__(self):
        self.bus = None
        self.fdroid_interface = None

    async def setup(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self.fdroid_interface = FDroidInterface()
        self.bus.export('/fdroid', self.fdroid_interface)

        await self.bus.request_name('io.FuriOS.AndroidStore')

        await self.bus.wait_for_disconnect()

async def main():
    service = AndroidStoreService()
    await service.setup()

if __name__ == "__main__":
    asyncio.run(main())
