"""Differential test of the trust-boundary CIDR code (NativeConfig::cidr_ok / in_cidr) against Python's ipaddress as oracle.
Seeds: Fat PS4 worker's [ip, cidr] edge pairs (one malformed element) and Slim PS4 worker's 21 validation strings, plus deterministic random generation.
Rules: dotted-decimal IPv4 only (4 octets 0..255, NO leading zeros, no signs/whitespace/hex); optional /N with N = 0 or 1-2 digits without leading zero, <= 32;
host bits in the CIDR are masked. An unparsable ip is never inside any network (fail closed)."""
import ipaddress, json, os, random, re, subprocess, unittest
A = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); R = os.path.dirname(A); DRV = os.environ.get('CIDR_DRIVER', '/tmp/cidr_driver')
FAT = [["0.0.0.0", "0.0.0.0/0"], ["255.255.255.255", "0.0.0.0/0"], ["192.168.1.1", "192.168.1.0/32"], ["192.168.1.0", "192.168.1.0/32"], ["192.168.1.255", "192.168.1.0/32"], ["192.168.1.0", "192.168.1.0/31"],
       ["192.168.1.1", "192.168.1.0/31"], ["192.168.1.254", "192.168.1.0/31"], ["192.168.1.255", "192.168.1.0/31"], ["192.168.3.1", "192.168.1.0/24"], ["192.168.1.77", "192.168.1.0/24"], ["192.168.001.001", "192.168.1.0/24"],
       ["256.0.0.0", "192.168.1.0/24"], ["192.168.1", "192.168.1.0/24"], ["", "192.168.1.0/24"], ["localhost", "127.0.0.0/8"], ["::1", "127.0.0.0/8"], ["127.0.0.1", "127.0.0.0/8"], ["192.168.1.77", "192.168.1.77/24"]]
SLIM = ["10.0.0.0/8", "192.168.1.0/24", "1.2.3.4", "10.0.0.0/33", "10.0.0.0/-1", "10.0.0.0/", "10.0.0.0/abc", "10.0.0.0/8x", "10.0.0.0/ 8", "10.0.0.0", "10.0.0", "10.0.0.0.0", "256.0.0.0/8", "999.0.0.0/8", "0.0.0.-1/8",
        "", "/", "10.0.0.0//8", "10.0.0.0/0000000008", "10.0.0.0 10.0.0.0/8", "0x7f.0.0.1/8", " 10.0.0.0/8", "10.0.0.0/8 ", "10.0.0.0./8", "010.0.0.0/8", "10.0.0.0/08", "10.0.0.0/32", "10.0.0.0/0", "+1.0.0.0/8", "1.2.3.4/٣"]
def ip4(s):
    if not re.fullmatch(r'(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}', s or '', re.ASCII): return None
    try: return ipaddress.IPv4Address(s)
    except ValueError: return None
def oracle_valid(c):
    a, sep, p = c.partition('/')
    if ip4(a) is None: return False
    return not sep or (re.fullmatch(r'0|[1-9][0-9]?', p, re.ASCII) is not None and int(p) <= 32)
def oracle_member(ip, c):
    if not oracle_valid(c) or ip4(ip) is None: return False
    a, sep, p = c.partition('/'); return ip4(ip) in ipaddress.ip_network(f'{a}/{p if sep else 32}', strict=False)
def run(ops):
    out = subprocess.run([DRV], input='\n'.join(json.dumps(o) for o in ops) + '\n', capture_output=True, text=True, timeout=120).stdout.split('\n')[:-1]
    assert len(out) == len(ops), (len(out), len(ops)); return out
def rand_ip(rnd):
    r = rnd.random()
    if r < 0.6: return '.'.join(str(rnd.choice([0, 1, 10, 127, 128, 168, 192, 254, 255, rnd.randrange(256)])) for _ in range(4))
    return rnd.choice(['', '1.2.3', '1.2.3.4.5', '256.1.1.1', '01.2.3.4', '1.2.3.04', '-1.2.3.4', '1.2.3.4 ', ' 1.2.3.4', '1.2..4', '0x1.2.3.4', 'a.b.c.d', '::1', '1.2.3.4/24', '١.٢.٣.٤', '1.2.3.4\x00', '1.2.3.' + '9' * 12])
def rand_cidr(rnd):
    ip = rand_ip(rnd); r = rnd.random()
    if r < 0.15: return ip
    pfx = rnd.choice([str(rnd.randrange(0, 33)), str(rnd.randrange(0, 33)), '33', '-1', '', 'abc', '08', '00', '0', '32', ' 8', '8 ', '+8', '1' * 12, '٣', '2\x00'])
    return f'{ip}/{pfx}'
class Cidr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DRV): subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined', '-I', os.path.join(A, 'src'), '-I', (os.path.join(A, 'vendor') if os.path.isdir(os.path.join(A, 'vendor')) else os.path.join(R, 'ps4-worker/vendor')), os.path.join(A, 'tests/cidr_driver.cpp'), '-o', DRV], check=True)
    def test_validity_differential(self):
        rnd = random.Random(7); cs = list(SLIM) + [c for _, c in FAT] + [rand_cidr(rnd) for _ in range(6000)]
        got = run([{'op': 'v', 'cidr': c} for c in cs]); bad = [(c, g, oracle_valid(c)) for c, g in zip(cs, got) if (g == '1') != oracle_valid(c)]
        self.assertEqual(bad[:12], [], f'{len(bad)} disagreements (cidr, cpp, oracle)')
    def test_membership_differential(self):
        rnd = random.Random(8); pairs = [(i, c) for i, c in FAT] + [(rand_ip(rnd), rand_cidr(rnd)) for _ in range(8000)]
        # make a good share of random pairs genuinely related so "inside" is exercised, not just "outside"
        for _ in range(3000):
            base = ip4(rand_ip(rnd)) or ipaddress.IPv4Address('10.1.2.3'); n = rnd.randrange(0, 33); net = ipaddress.ip_network(f'{base}/{n}', strict=False)
            off = rnd.choice([0, 1, net.num_addresses - 1, rnd.randrange(net.num_addresses), net.num_addresses, -1])
            try: pairs.append((str(ipaddress.IPv4Address(int(net.network_address) + off)), f'{base}/{n}'))   # inside, edges, and just-outside (above/below) the network
            except ValueError: pass
        got = run([{'op': 'm', 'ip': i, 'cidr': c} for i, c in pairs]); bad = [(i, c, g, oracle_member(i, c)) for (i, c), g in zip(pairs, got) if (g == '1') != oracle_member(i, c)]
        self.assertEqual(bad[:12], [], f'{len(bad)} disagreements (ip, cidr, cpp, oracle) of {len(pairs)}')
        self.assertGreater(sum(oracle_member(i, c) for i, c in pairs), 800)    # the "inside" branch was really exercised
if __name__ == '__main__': unittest.main()
