import argparse
import shlex
import shutil
import subprocess
from pathlib import Path
from . import importer
from .service import Map, make_server


def main():
    parser = argparse.ArgumentParser(description='SightMesh 定位地图包与只读查询服务')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('import', help='从 sim 导入版本化定位地图')
    build.add_argument('--source', required=True, type=Path)
    build.add_argument('--output', default=Path('data/maps'), type=Path)
    build.add_argument('--config', default=Path('config/industrial-park.json'), type=Path)
    build.add_argument('--engine', choices=('python', 'cpp'), default='python')
    serve = sub.add_parser('serve', help='提供指定版本的查询和数据下载')
    serve.add_argument('--map', type=Path)
    serve.add_argument('--source', type=Path, help='服务启动时在线导入源地图并命中内容缓存')
    serve.add_argument('--output', default=Path('data/maps'), type=Path)
    serve.add_argument('--config', default=Path('config/industrial-park.json'), type=Path)
    serve.add_argument('--engine', choices=('python', 'cpp'), default='python')
    serve.add_argument('--bind', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    serve.add_argument('--base-url', help='渲染设备可访问的中心地址；启动时输出渲染配置')
    render = sub.add_parser('render-env', help='从地图版本生成渲染设备的环境变量')
    render.add_argument('--map', type=Path, required=True)
    render.add_argument('--base-url', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'import':
            import json
            if args.engine == 'cpp':
                binary = shutil.which('sightmesh-map-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-cpp')
                if not Path(binary).is_file():
                    raise ValueError('C++ importer is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
                subprocess.run([str(binary), '--source', str(args.source), '--output', str(args.output), '--config', str(args.config)], check=True)
            else:
                print(importer.build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8'))))
        else:
            if args.engine == 'cpp':
                binary = shutil.which('sightmesh-map-server-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-server-cpp')
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
                import json
                if args.engine == 'cpp':
                    binary = shutil.which('sightmesh-map-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-cpp')
                    if not Path(binary).is_file():
                        raise ValueError('C++ importer is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
                    completed = subprocess.run([str(binary), '--source', str(args.source), '--output', str(args.output), '--config', str(args.config)], check=True, text=True, capture_output=True)
                    print(completed.stderr, end='', flush=True)
                    map_path = Path(completed.stdout.strip())
                else:
                    map_path = importer.build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8')))
            elif args.map:
                map_path = args.map
            else:
                raise ValueError('serve requires --map or --source')
            data = Map(map_path)
            if args.command == 'render-env' or args.base_url:
                for name, value in data.cesium.render_environment(args.base_url).items():
                    print(f'export {name}={shlex.quote(value)}', flush=True)
            if args.command == 'render-env':
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
