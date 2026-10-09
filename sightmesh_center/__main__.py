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


def default_render_config() -> Path:
    workspace = Path(__file__).resolve().parents[2]
    return workspace / 'sightmesh-render/config/render.ini'


def update_render_config(target: Path, data: Map, base_url: str) -> None:
    render_config = configparser.ConfigParser(interpolation=None)
    render_config.optionxform = str
    with target.open(encoding='utf-8') as stream:
        render_config.read_file(stream)
    updates = data.cesium.render_configuration(base_url)
    lines = target.read_text(encoding='utf-8').splitlines()
    in_runtime = False
    for index, line in enumerate(lines):
        if line.strip().startswith('['):
            in_runtime = line.strip() == '[runtime]'
        match = re.match(r'\s*([A-Z0-9_]+)\s*=', line) if in_runtime else None
        if match and match[1] in updates:
            lines[index] = f'{match[1]} = {updates.pop(match[1])}'
    if updates:
        start = next(i for i, line in enumerate(lines) if line.strip() == '[runtime]') + 1
        end = next((i for i in range(start, len(lines)) if lines[i].strip().startswith('[')), len(lines))
        lines[end:end] = [f'{key} = {value}' for key, value in updates.items()]
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'Render configuration updated: {target}', flush=True)


def import_source(source: Path, output: Path, config: Path, engine: str) -> Path:
    if engine == 'cpp':
        binary = shutil.which('sightmesh-map-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-cpp')
        if not Path(binary).is_file():
            raise ValueError('C++ importer is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
        result = subprocess.run([str(binary), '--source', str(source), '--output', str(output),
                                 '--config', str(config)], check=True, capture_output=True, text=True)
        output_path = result.stdout.strip().splitlines()[-1]
        # std::filesystem::path is streamed as a quoted string by the importer.
        return Path(json.loads(output_path) if output_path.startswith('"') else output_path)
    return importer.build(source, output, json.loads(config.read_text(encoding='utf-8')))


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
    serve.add_argument('--render-config', type=Path, default=default_render_config())
    ingest = sub.add_parser('ingest', help='独立启动可选 TrackEvent/Blob 持久接收服务')
    ingest_settings = settings.get('ingest', {})
    if not isinstance(ingest_settings, dict):
        raise ValueError('runtime.ingest must be an object')
    ingest.add_argument('--bind', default=ingest_settings.get('bind', '127.0.0.1'))
    ingest.add_argument('--port', type=int, default=ingest_settings.get('port', 18081))
    ingest.add_argument('--db', type=Path, default=Path(ingest_settings.get('db', 'data/track-inbox.sqlite3')))
    ingest.add_argument('--token-file', type=Path, default=Path(ingest_settings['token_file']) if ingest_settings.get('token_file') else None)
    associate = sub.add_parser('associate', help='从已提交 TrackEvent 重建 Tracklet 和全局关联 V1')
    associate.add_argument('--db', type=Path, default=Path(
        settings.get('ingest', {}).get('db', 'data/track-inbox.sqlite3')))
    associate.add_argument('--policy', type=Path, help='独立 JSON 策略文件；默认使用 center.json 的 association')
    associate.add_argument('--solver', choices=('greedy', 'ortools', 'clique'), default='greedy')
    associate.add_argument('--fusion', choices=('off', 'ci'), default='off',
                           help='显式开启实验性 CI 位置估计；仅存储旁路结果，不覆盖正式位置')
    associate.add_argument('--window-factors', action='store_true',
                           help='显式运行滑窗方位/位置因子估计；须经独立性/几何秩验证，输出仅作旁路')
    associate.add_argument('--max-events', type=int, default=200000)
    associate.add_argument('--watch', action='store_true', help='按周期重新评估，不是系统守护服务')
    associate.add_argument('--interval', type=float, default=2.0)
    global_state = sub.add_parser('global-state', help='只读查询已提交的 GlobalTrack 快照')
    global_state.add_argument('--db', type=Path, default=Path(
        settings.get('ingest', {}).get('db', 'data/track-inbox.sqlite3')))
    check = sub.add_parser('check-config', help='验证 center 的统一配置文件')
    check.add_argument('--config', type=Path, default=config_path)
    render = sub.add_parser('render-config', help='把地图资源地址和锚点写入 render 的唯一配置文件')
    render.add_argument('--map', type=Path, default=Path(settings['map']) if settings.get('map') else None)
    render.add_argument('--source', type=Path, default=Path(settings['source']) if settings.get('source') else None)
    render.add_argument('--output', type=Path, default=Path(settings.get('output', 'data/maps')))
    render.add_argument('--config', type=Path, default=config_path)
    render.add_argument('--engine', choices=('python', 'cpp'), default=settings.get('engine', 'python'))
    render.add_argument('--base-url', default=settings.get('base_url'))
    render.add_argument('--render-config', type=Path, default=default_render_config())
    args = parser.parse_args()
    if args.command == 'associate':
        import time
        from .association_v1 import AssociationPolicy
        from .global_tracks import GlobalTrackRepository
        from .fusion_ci import FusionPolicy
        from .window_factor_graph import WindowPolicy
        try:
            raw_policy = (json.loads(args.policy.read_text(encoding='utf-8'))
                          if args.policy else configuration.get('association', {}))
            policy = AssociationPolicy.from_dict(raw_policy)
            if not 0.1 <= args.interval <= 3600 or args.max_events <= 0:
                raise ValueError('invalid association interval/event limit')
            fusion_policy = (FusionPolicy.from_dict(
                configuration.get('fusion', {}))
                if args.fusion == 'ci' else None)
            window_policy = (WindowPolicy.from_dict(
                configuration.get('window_factor_graph', {}))
                if args.window_factors else None)
            repository = GlobalTrackRepository(args.db)
            while True:
                state = repository.recompute(
                    policy, args.solver, args.max_events,
                    fusion_policy=fusion_policy, window_policy=window_policy)
                print(json.dumps({
                    'world_revision': state['world_revision'],
                    'global_track_count': len(state['global_tracks']),
                    'candidate_count': state['candidate_count'],
                    'selected_pair_count': state['selected_pair_count'],
                    'experimental_fusion_accepted':
                        state.get('experimental_fusion_accepted', 0),
                    'fusion_mode': state.get('fusion_mode', 'off'),
                    'window_factor_mode': state.get('window_factor_mode', 'off'),
                    'experimental_window_accepted':
                        state.get('experimental_window_accepted', 0),
                    'last_identity_revision': state['last_identity_revision'],
                }, ensure_ascii=False), flush=True)
                if not args.watch:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(2, f'中心关联失败: {error}\\n')
        return
    if args.command == 'global-state':
        from .global_tracks import GlobalTrackRepository
        try:
            print(json.dumps(GlobalTrackRepository(args.db).latest(),
                             ensure_ascii=False, indent=2))
        except (OSError, ValueError) as error:
            parser.exit(2, f'查询失败: {error}\\n')
        return
    if args.command == 'ingest':
        from .track_ingest_server import serve_inbox
        try:
            with serve_inbox(args.bind, args.port, args.db, args.token_file) as server:
                print(f'Durable TrackEvent Inbox: {args.bind}:{server.server_port}', flush=True)
                server.serve_forever()
        except KeyboardInterrupt:
            pass
        except (OSError, ValueError) as error:
            parser.exit(2, f'Inbox 启动失败: {error}\n')
        return
    if args.command == 'check-config':
        if settings.get('engine', 'python') not in ('python', 'cpp') or not isinstance(settings.get('port', 8080), int) or not 1 <= settings.get('port', 8080) <= 65535:
            parser.exit(1, '配置无效: engine 或 port\n')
        print(f'Configuration loaded: {config_path}')
        return
    if args.command in ('import', 'serve') and '--map' in sys.argv and '--source' not in sys.argv:
        args.source = None
    try:
        if args.command == 'import':
            if not args.source:
                raise ValueError('import requires source in center.json or --source')
            print(import_source(args.source, args.output, args.config, args.engine))
        elif args.command == 'render-config':
            map_path = args.map or (import_source(args.source, args.output, args.config, args.engine)
                                    if args.source else None)
            if not map_path:
                raise ValueError('render-config requires map or source in center.json, or --map/--source')
            data = Map(map_path)
            update_render_config(args.render_config, data,
                                 args.base_url or settings.get('base_url', ''))
        else:  # serve
            if args.source:
                map_path = import_source(args.source, args.output, args.config, args.engine)
            elif args.map:
                map_path = args.map
            else:
                raise ValueError('serve/render-config requires map in center.json or --map/--source')
            data = Map(map_path)
            update_render_config(args.render_config, data,
                                 args.base_url or settings.get('base_url', ''))
            if args.engine == 'cpp':
                binary = settings.get('server') or shutil.which('sightmesh-map-server-cpp') or (Path(__file__).resolve().parent.parent/'build/cpp/sightmesh-map-server-cpp')
                if not Path(binary).is_file():
                    raise ValueError('C++ map server is not built; run cmake -S cpp -B build/cpp && cmake --build build/cpp')
                command = [str(binary), '--bind', args.bind, '--port', str(args.port)]
                if args.source:
                    # The C++ server watches source changes and refreshes its map.
                    command += ['--source', str(args.source), '--output', str(args.output),
                                '--config', str(args.config)]
                else:
                    command += ['--map', str(map_path)]
                subprocess.run(command, check=True)
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
