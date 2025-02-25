# src/cosmic/cli.py
import click
import subprocess
import json
import time
from typing import Optional

def create_namespace_if_not_exists(namespace: str):
    try:

        # Check if namespace exists
        result = run_command(f"kubectl get namespace {namespace}")
        # Namespace exists, just return without doing anything
        return
    except subprocess.CalledProcessError:
        # This specific error means the namespace wasn't found
        run_command(f"kubectl create namespace {namespace}")
        click.echo(f"Created namespace {namespace}")
    except Exception as e:
        # Handle any other unexpected errors
        click.echo(f"Unexpected error: {str(e)}")
        raise click.Abort()

def run_command(command: str, shell: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command and handle errors."""
    try:
        if shell:
            result = subprocess.run(command, shell=True, check=True, text=True, capture_output=True)
        else:
            result = subprocess.run(command.split(), check=True, text=True, capture_output=True)
        return result
    except subprocess.CalledProcessError as e:
        click.echo(f"Error executing command: {command}")
        click.echo(f"Error output: {e.stderr}")
        raise click.Abort()

def check_prerequisites():
    """Check if required tools are installed."""
    required_tools = ['kind', 'docker', 'kubectl', 'helm']
    for tool in required_tools:
        try:
            run_command(f"which {tool}")
        except:
            click.echo(f"Error: {tool} is not installed or not in PATH")
            raise click.Abort()

@click.group()
def cli():
    """Cosmic CLI tool for managing Kind clusters with Cilium and Multus."""
    pass

@cli.command()
def check():
    """Check prerequisites."""
    try:
        check_prerequisites()
        click.echo("✅ All prerequisites are installed!")
    except click.Abort:
        click.echo("❌ Prerequisites check failed")

@cli.command()
def cleanup():
    """Delete existing kind cluster and registry."""
    cluster_name = 'kind'
    reg_name = 'kind-registry'
    
    # Check and delete kind cluster
    result = run_command("kind get clusters")
    if cluster_name in result.stdout:
        click.echo(f"Deleting existing Kind cluster '{cluster_name}'...")
        run_command(f"kind delete cluster --name {cluster_name}")
    
    # Check and delete registry container
    try:
        docker_inspect = run_command(f"docker inspect {reg_name}")
        container_info = json.loads(docker_inspect.stdout)
        if container_info and container_info[0]["State"]["Running"]:
            click.echo(f"Deleting existing registry container '{reg_name}'...")
            run_command(f"docker stop {reg_name}")
            run_command(f"docker rm {reg_name}")
    except:
        pass
    
    click.echo("✅ Cleanup completed!")

@cli.command()
def setup_registry():
    """Create and setup local registry."""
    reg_name = 'kind-registry'
    reg_port = '5001'
    
    # Create registry container
    click.echo("Setting up local registry...")
    run_command(
        f"docker run -d --restart=always -p 127.0.0.1:{reg_port}:5000 "
        f"--network bridge --name {reg_name} registry:2",
        shell=True
    )
    click.echo("✅ Registry setup completed!")

@cli.command()
def create_cluster():
    """Create Kind cluster with custom configuration."""
    click.echo("Creating Kind cluster...")
    cluster_config = """
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
    protocol: TCP
  - containerPort: 443
    hostPort: 443
    protocol: TCP
- role: worker
- role: worker
- role: worker
networking:
  disableDefaultCNI: true
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
"""
    with open("kind-config.yaml", "w") as f:
        f.write(cluster_config)
    
    run_command("kind create cluster --config kind-config.yaml")
    click.echo("✅ Cluster created successfully!")

@cli.command()
def configure_registry():
    """Configure registry in the cluster."""
    reg_name = 'kind-registry'
    reg_port = '5001'
    
    click.echo("Configuring registry in the cluster...")
    
    # Add registry config to nodes
    nodes = run_command("kind get nodes").stdout.strip().split('\n')
    for node in nodes:
        registry_dir = f"/etc/containerd/certs.d/localhost:{reg_port}"
        run_command(f"docker exec {node} mkdir -p {registry_dir}", shell=True)
        hosts_toml = f'[host."http://{reg_name}:5000"]'
        run_command(f'echo "{hosts_toml}" | docker exec -i {node} tee {registry_dir}/hosts.toml', shell=True)
    
    # Connect registry to cluster network
    try:
        run_command(f"docker network connect kind {reg_name}", shell=True)
    except:
        click.echo("Registry already connected to network")
    
    # Create ConfigMap
    config_map = f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:{reg_port}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
"""
    with open("registry-config.yaml", "w") as f:
        f.write(config_map)
    run_command("kubectl apply -f registry-config.yaml")
    
    click.echo("✅ Registry configured successfully!")

@cli.command()
def install_cilium():
    """Install Cilium CNI."""
    click.echo("Installing Cilium...")
    
    # Pull and load Cilium image
    run_command("docker pull quay.io/cilium/cilium:v1.17.1")
    run_command("kind load docker-image quay.io/cilium/cilium:v1.17.1")
    
    # Add Helm repo and install Cilium
    run_command("helm repo add cilium https://helm.cilium.io/")
    run_command(
        "helm upgrade --install cilium cilium/cilium --version 1.17.1 "
        "--namespace kube-system "
        "--set image.pullPolicy=IfNotPresent "
        "--set ipam.mode=kubernetes "
        "--set cni.install=true "
        "--set cni.chainingMode=generic-veth "
        "--set hubble.relay.enabled=true "
        "--set hubble.ui.enabled=true "
        "--set kubeProxyReplacement=true "
        "--set loadBalancer.l7.backend=envoy "
        "--set arp.enabled=true",

        shell=True
    )
    
    # Wait for Cilium to be ready
    run_command("kubectl -n kube-system rollout status daemonset/cilium")
    click.echo("✅ Cilium installed successfully!")

@cli.command()
def install_multus():
    """Install Multus CNI."""
    click.echo("Installing Multus...")
    
    run_command(
        "kubectl apply -f "
        "https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/master/deployments/multus-daemonset.yml"
    )
    
    # Wait for Multus to be ready
    run_command("kubectl -n kube-system rollout status daemonset/kube-multus-ds")
    click.echo("✅ Multus installed successfully!")

@cli.command()
def install_argocd():
    """Install Argo CD."""
    click.echo("Installing ArgoCD...")
    
    # Create namespace
    create_namespace_if_not_exists("argocd")
    
    # Install ArgoCD
    run_command(
        "kubectl apply -f "
        "https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml "
        "-n argocd"
    )
    
    # Wait for ArgoCD to be ready
    run_command("kubectl -n argocd rollout status deployment/argocd-server")
    
    # Change service type to LoadBalancer
    patch_data = {
        "spec": {
            "type": "LoadBalancer"
        }
    }
    run_command(
        f"kubectl patch svc argocd-server -n argocd -p '{json.dumps(patch_data)}'",
        shell=True
    )
    
    # Get initial admin password
    try:
        password = run_command(
            "kubectl -n argocd get secret argocd-initial-admin-secret "
            "-o jsonpath='{.data.password}' | base64 -d",
            shell=True
        ).stdout.strip()
        click.echo(f"\n📝 Initial admin password: {password}")
    except:
        click.echo("\n⚠️  Could not retrieve initial admin password")
    
    click.echo("✅ ArgoCD installed successfully!")
    click.echo("\nTo access ArgoCD UI, run:")
    click.echo("cosmic port-forward-argocd")

@cli.command()
def port_forward_argocd():
    """Port forward ArgoCD server to localhost:8080."""
    click.echo("Starting port forward for ArgoCD...")
    click.echo("Access the UI at: https://localhost:8080")
    click.echo("Use username: admin")
    click.echo("Press Ctrl+C to stop")
    try:
        run_command(
            "kubectl port-forward svc/argocd-server -n argocd 8080:443",
            shell=True
        )
    except KeyboardInterrupt:
        click.echo("\nPort forward stopped")


@cli.command()
def verify():
    """Verify the installation."""
    click.echo("Verifying installation...")
    
    # Check Cilium pods
    cilium_pods = run_command("kubectl get pods -n kube-system -l k8s-app=cilium")
    click.echo("\nCilium pods:")
    click.echo(cilium_pods.stdout)
    
    # Check Multus pods
    multus_pods = run_command("kubectl get pods -n kube-system -l app=multus")
    click.echo("\nMultus pods:")
    click.echo(multus_pods.stdout)
    
    click.echo("✅ Verification completed!")

@cli.command()
def setup_all():
    """Run the complete setup process."""
    commands = [
        ('Prerequisites Check', check),
        ('Cleanup', cleanup),
        ('Setup Registry', setup_registry),
        ('Create Cluster', create_cluster),
        ('Configure Registry', configure_registry),
        ('Install Cilium', install_cilium),
        ('Install Multus', install_multus),
        ('Verify Installation', verify)
    ]
    
    for step_name, command in commands:
        click.echo(f"\n📍 Starting {step_name}...")
        try:
            command()
        except click.Abort:
            click.echo(f"❌ Setup failed during {step_name}")
            return
    
    click.echo("\n✨ Complete setup finished successfully!")

if __name__ == "__main__":
    cli()