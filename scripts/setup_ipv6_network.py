"""Create an optional Docker IPv6 network with a stable host SNAT address.

Uses the selected Docker context, including Docker SSH contexts on Windows.
Existing networks are checked and never replaced by this command.
"""
import argparse
import ipaddress
import json
import subprocess


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Public IPv6 already configured on the Docker host')
    parser.add_argument('--name', default='video-fixed-ipv6')
    parser.add_argument('--subnet', default='fd42:76b7:2026::/64')
    parser.add_argument('--docker', default='docker', help='Docker executable, optionally an absolute Windows path')
    args = parser.parse_args(argv)
    source = ipaddress.IPv6Address(args.source)
    subnet = ipaddress.IPv6Network(args.subnet)
    if not source.is_global or not subnet.subnet_of(ipaddress.IPv6Network('fc00::/7')):
        parser.error('--source must be a public IPv6 and --subnet must be a private ULA network')
    inspected = subprocess.run([args.docker, 'network', 'inspect', args.name], capture_output=True, text=True)
    if inspected.returncode == 0:
        network = json.loads(inspected.stdout)[0]
        subnets = [item.get('Subnet') for item in network['IPAM']['Config']]
        if (not network.get('EnableIPv6') or str(subnet) not in subnets
                or network.get('Options', {}).get('com.docker.network.host_ipv6') != str(source)):
            parser.error('Existing network differs from requested configuration; it was left unchanged')
        print('IPv6 network already matches:', args.name, 'egress:', source)
        return
    # Do not treat an unavailable Docker daemon as an absent network.
    subprocess.run([args.docker, 'info', '--format', '{{.ServerVersion}}'], check=True)
    subprocess.run([args.docker, 'network', 'create', '--ipv6', '--subnet', str(subnet),
                    '--opt', 'com.docker.network.host_ipv6=' + str(source), args.name], check=True)
    print('IPv6 network created:', args.name, 'egress:', source)


if __name__ == '__main__':
    main()
