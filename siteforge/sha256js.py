"""Plain-JS SHA-256 (ES2020, no crypto.subtle) used by A10 pages."""


def _icbrt(n: int) -> int:
    x = 1 << ((n.bit_length() + 2) // 3)
    while True:
        y = (2 * x + n // (x * x)) // 3
        if y >= x:
            break
        x = y
    while x ** 3 > n:
        x -= 1
    while (x + 1) ** 3 <= n:
        x += 1
    return x


def _primes(k):
    out, n = [], 2
    while len(out) < k:
        if all(n % p for p in out):
            out.append(n)
        n += 1
    return out


K = [_icbrt(p << 96) & 0xFFFFFFFF for p in _primes(64)]

SHA256_JS = """
function sha256(s){var K=[%s];
var H=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
function R(x,n){return (x>>>n)|(x<<(32-n));}
var b=[],i,t;for(i=0;i<s.length;i++)b.push(s.charCodeAt(i)&255);
var l=b.length*8;b.push(0x80);while(b.length%%64!=56)b.push(0);
for(i=7;i>=0;i--)b.push(i>3?0:(l>>>(i*8))&255);
var w=new Array(64);
for(var o=0;o<b.length;o+=64){
for(t=0;t<16;t++)w[t]=(b[o+4*t]<<24)|(b[o+4*t+1]<<16)|(b[o+4*t+2]<<8)|b[o+4*t+3];
for(t=16;t<64;t++){var x=w[t-15],y=w[t-2];
w[t]=(w[t-16]+(R(x,7)^R(x,18)^(x>>>3))+w[t-7]+(R(y,17)^R(y,19)^(y>>>10)))|0;}
var a=H[0],bb=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
for(t=0;t<64;t++){var t1=(h+(R(e,6)^R(e,11)^R(e,25))+((e&f)^(~e&g))+K[t]+w[t])|0;
var t2=((R(a,2)^R(a,13)^R(a,22))+((a&bb)^(a&c)^(bb&c)))|0;
h=g;g=f;f=e;e=(d+t1)|0;d=c;c=bb;bb=a;a=(t1+t2)|0;}
H[0]=(H[0]+a)|0;H[1]=(H[1]+bb)|0;H[2]=(H[2]+c)|0;H[3]=(H[3]+d)|0;
H[4]=(H[4]+e)|0;H[5]=(H[5]+f)|0;H[6]=(H[6]+g)|0;H[7]=(H[7]+h)|0;}
return H.map(function(v){return ("00000000"+(v>>>0).toString(16)).slice(-8);}).join("");}
""" % ",".join(hex(k) for k in K)
