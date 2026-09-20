# Local development only. Production updates use deploy/kubernetes/manage.py.
config.define_bool('federation')
config.define_string('env-file')
options = config.parse()
args = ['--federation'] if options.get('federation', False) else []
if options.get('env-file', ''):
    args += ['--env-file', options['env-file']]

watch_file('deploy/kubernetes')
watch_file('deploy/Caddyfile')
watch_file(options.get('env-file', '.env'))

metadata = decode_json(local(['python3', 'deploy/kubernetes/dev.py', 'metadata'] + args, quiet=True))
if k8s_context() != metadata['context']:
    fail('Wrong Kubernetes context. Start this checkout using make dev or make dev-federation.')
allow_k8s_contexts(metadata['context'])
analytics_settings(False)
disable_snapshots()
# Other applications share this Docker daemon; never prune their images.
docker_prune_settings(disable=True)
update_settings(max_parallel_updates=1)

custom_build('kaede-backend', ['python3', 'deploy/kubernetes/dev.py', 'build-image', '--service=backend'] + args, deps=['backend'], ignore=['backend/.venv', 'backend/.ruff_cache', 'backend/.pytest_cache', 'backend/.mypy_cache', 'backend/**/__pycache__'], disable_push=True, skips_local_docker=True, live_update=[
    fall_back_on(['backend/pyproject.toml', 'backend/uv.lock', 'backend/Dockerfile', 'backend/migrations']),
    sync('backend/app', '/workspace/app'),
    sync('backend/scripts', '/workspace/scripts'),
])
custom_build('kaede-frontend', ['python3', 'deploy/kubernetes/dev.py', 'build-image', '--service=frontend'] + args, deps=['frontend'], ignore=['frontend/node_modules', 'frontend/.svelte-kit', 'frontend/build'], disable_push=True, skips_local_docker=True, live_update=[
    fall_back_on(['frontend/package.json', 'frontend/pnpm-lock.yaml', 'frontend/Dockerfile', 'frontend/patches']),
    sync('frontend/src', '/workspace/src'),
    sync('frontend/static', '/workspace/static'),
])

def resource_name(obj):
    return obj.name + '-' + obj.namespace

workload_to_resource_function(resource_name)
k8s_yaml(local(['python3', 'deploy/kubernetes/dev.py', 'render'] + args, quiet=True))
for ns in metadata['namespaces']:
    k8s_resource('preflight-' + ns)
    k8s_resource('migrate-' + ns, resource_deps=['preflight-' + ns, 'postgres-' + ns, 'dragonfly-' + ns])
    k8s_resource('storage-init-' + ns, resource_deps=['preflight-' + ns])
    for role in ['api', 'gateway', 'worker', 'scheduler']:
        k8s_resource(role + '-' + ns, resource_deps=['migrate-' + ns, 'storage-init-' + ns])
    k8s_resource('caddy-' + ns, resource_deps=['api-' + ns, 'gateway-' + ns, 'frontend-' + ns])
