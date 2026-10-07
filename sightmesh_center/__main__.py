import argparse
import json
import sys
import configparser
import re
import shutil
import subprocess
from pathlib import Path
from . import importer
from .service import Map, make_server


def main():
    config_path = Path(__file__).resolve().parents[1] / 'config/center.json'
    for i, item in enumerate(sys.argv[1:], 1):
        if item == '--config' and i + 1 < len(sys.argv):
            config_path = Path(sys.argv[i + 1])
        elif item.startswith('--config='):
            config_path = Path(item.split('=', 1)[1])
    try:
        configuration = json.loads(config_path.read_text(encoding='utf-8'))
        settings = configuration.get('runtime', {})
        if not isinstance(settings, dict):
            raise ValueError('runtime must be an object')
    except (OSError, ValueError) as error:
        if '--help' not in sys.argv and '-h' not in sys.argv:
            raise SystemExit(f'配置读取失败: {error}')
        settings = {}
    parser = argparse.ArgumentParser(description='SightMesh 定位地图包与只读查询服务')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('import', help='从 sim 导入版本化定位地图')
    build.add_argument('--source', default=Path(settings['source']) if settings.get('source') else None, type=Path)
    build.add_argument('--output', default=Path(settings.get('output', 'data/maps')), type=Path)
    build.add_argument('--config', default=config_path, type=Path)
    build.add_argument('--engine', choices=('python', 'cpp'), default=settings.get('engine', 'python'))
    serve = sub.add_parser('serve', help='提供指定版本的查询和数据下载')
    serve.add_argument('--map', type=Path, default=Path(settings['map']) if settings.get('map') else None)
    serve.add_argument('--source', type=Path, default=Path(settings['source']) if settings.get('source') and not settings.get('map') else None, help='服务启动时在线导入源地图并命中内容缓存')
    serve.add_argument('--output', default=Path(settings.get('output', 'data/maps')), type=Path)
    serve.add_argument('--config', default=config_path, type=Path)
    serve.add_argument('--engine', choices=('python', 'cpp'), default=settings.get('engine', 'python'))
    serve.add_argument('--bind', default=settings.get('bind', '127.0.0.1'))
    serve.add_argument('--port', type=int, default=settings.get('port', 8080))
    serve.add_argument('--base-url', default=settings.get('base_url'), help='渲染设备可访问的中心地址（保存在统一配置中）')
    check = sub.add_parser('check-config', help='验证 center 的统一配置文件')
    check.add_argument('--config', type=Path, default=config_path)
    render = sub.add_parser('render-config', help='把地图资源地址和锚点写入 render 的唯一配置文件')
    render.add_argument('--map', type=Path, required=True)
    render.add_argument('--base-url', required=True)
    render.add_argument('--render-config', type=Path, default=Path('../sightmesh-render/config/render.ini'))
    args = parser.parse_args()
    if args.command == 'check-config':
        if settings.get('engine', 'python') not in ('python', 'cpp') or not isinstance(settings.get('port', 8080), int) or not 1 <= settings.get('port', 8080) <= 65535:
            parser.exit(1, '配置无效: engine 或 port\n')
        print(f'Configuration loaded: {config_path}')
        return
    if args.command == 'render-config':
        args.engine = 'python'
        args.source = None
    elif '--map' in sys.argv and '--source' not in sys.argv:
        args.source = None
    try:
        if args.command == 'import':
            if not args.source:
                raise ValueError('import requires source in center.json or --source')
            if args.engine == 'cpp':
                binary = shutil.which('sightmesh-map-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-cpp')
                if not Path(binary).is_file():
                    raise ValueError('C++ importer is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
                subprocess.run([str(binary), '--source', str(args.source), '--output', str(args.output), '--config', str(args.config)], check=True)
            else:
                print(importer.build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8'))))
        else:
            if args.engine == 'cpp':
                binary = settings.get('server') or shutil.which('sightmesh-map-server-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-server-cpp')
                if not Path(binary).is_file():
                    raise ValueError('C++ map server is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
                command = [str(binary), '--bind', args.bind, '--port', str(args.port)]
                if args.source:
                    command += ['--source', str(args.source), '--output', str(args.output), '--config', str(args.config)]
                elif args.map:
                    command += ['--map', str(args.map)]
                else:
                    raise ValueError('serve requires --map or --source')
                subprocess.run(command, check=True)
                return
            if args.source:
                map_path = importer.build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8')))
            elif args.map:
                map_path = args.map
            else:
                raise ValueError('serve requires --map or --source')
            data = Map(map_path)
            if args.command == 'render-config':
                target = args.render_config
                render_config = configparser.ConfigParser(interpolation=None)
                render_config.optionxform = str
                with target.open(encoding='utf-8') as stream:
                    render_config.read_file(stream)
                updates = data.cesium.render_configuration(args.base_url)
                lines = target.read_text(encoding='utf-8').splitlines()
                in_runtime = False
                for index, line in enumerate(lines):
                    if line.strip().startswith('['):
                        in_runtime = line.strip() == '[runtime]'
                    match = re.match(r'\s*([A-Z0-9_]+)\s*=', line) if in_runtime else None
                    if match and match[1] in updates:
                        lines[index] = f'{match[1]} = {updates.pop(match[1])}'
                if updates:
                    # Append missing runtime keys before a following section.
                    start = next(i for i, line in enumerate(lines) if line.strip() == '[runtime]') + 1
                    end = next((i for i in range(start, len(lines)) if lines[i].strip().startswith('[')), len(lines))
                    lines[end:end] = [f'{key} = {value}' for key, value in updates.items()]
                target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                print(f'Render configuration updated: {target}', flush=True)
                return
            with make_server(data, args.bind, args.port) as server:
                print(f'Map {data.manifest["map_revision"]}: {args.bind}:{server.server_port}', flush=True)
                server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, KeyError, IndexError, TypeError) as error:
        parser.exit(1, f'地图操作失败: {error}\n')


if __name__ == '__main__':
    main()
