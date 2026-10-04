import argparse
import shlex
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
    serve = sub.add_parser('serve', help='提供指定版本的查询和数据下载')
    serve.add_argument('--map', type=Path, required=True)
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
            print(importer.build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8'))))
        else:
            data = Map(args.map)
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
