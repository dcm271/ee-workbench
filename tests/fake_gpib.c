/* A stand-in for libgpib with a small bus, to test linux_gpib.py through the
   real C calling convention. Device descriptors are 100 + address.
   Address 5: an SCPI supply. 9: nobody. 12: there, but never answers. */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static __thread int sta, err;
static __thread long cnt;
static char pending[32][200000];
static long len[32], pos[32];
static int tmo[32], onl = 0;

static int done(int bits, int e, long n) { sta = 0x100 | bits; err = e; cnt = n; return sta; }
int ThreadIbsta(void) { return sta; }
int ThreadIberr(void) { return err; }
long ThreadIbcntl(void) { return cnt; }
void ibvers(char **v) { *v = "4.3.6-fake"; }
const char *gpib_error_string(int e) { return e == 2 ? "ENOL 2: No listeners" : e == 6 ? "EABO 6: Operation aborted" : "EDVR 0: OS error"; }

int ibdev(int b, int pad, int sad, int t, int eot, int eos)
{ if (b) { done(0x8000, 7, 0); return -1; } tmo[pad] = t; onl++; return 100 + pad; }
int ibfind(const char *n) { if (strcmp(n, "gpib0")) { done(0x8000, 0, 2); return -1; } return 0; }
int ibonl(int ud, int on) { onl--; return done(0, 0, 0); }
int open_count(void) { return onl; }
int ibask(int ud, int o, int *v) { *v = o == 3 ? tmo[ud - 100] : o == 1 ? (ud >= 100 ? ud - 100 : 0) : 0; return done(0, 0, 0); }
int ibconfig(int ud, int o, int v) { return done(0, 0, 0); }
int ibtmo(int ud, int v) { tmo[ud - 100] = v; return done(0, 0, 0); }
int ibwrt(int ud, const void *buf, long n)
{
	int p = ud - 100; char *out = pending[p];
	if (p == 9) return done(0x8000, 2, 0);
	len[p] = pos[p] = 0;
	if (p == 5 && !strncmp(buf, "*IDN?", 5)) len[p] = sprintf(out, "FAKE,66321D,0,1\n");
	if (p == 5 && !strncmp(buf, "BLOCK?", 6)) {
		len[p] = sprintf(out, "#6100000");
		for (long i = 0; i < 100000; i++) out[len[p]++] = (char)(i * 7);
		out[len[p]++] = '\n';
	}
	return done(0, 0, n);
}
int ibrd(int ud, void *buf, long n)
{
	int p = ud - 100; long k = len[p] - pos[p];
	if (k <= 0) return done(0x8000 | 0x4000, 6, 0);
	if (k > n) k = n;
	memcpy(buf, pending[p] + pos[p], k); pos[p] += k;
	return done(pos[p] == len[p] ? 0x2000 : 0, 0, k);
}
int ibclr(int ud) { len[ud - 100] = pos[ud - 100] = 0; return done(0, 0, 0); }
int ibrsp(int ud, char *s) { *s = (char)0xC0; return done(0, 0, 0); }
int ibln(int ud, int pad, int sad, short *f) { *f = (pad == 5 || pad == 12); return done(0, 0, 0); }
int iblines(int ud, short *l) { *l = (short)0x20FF; return done(0, 0, 0); }
int ibcmd(int ud, const void *c, long n) { return done(0, 0, n); }
int ibcac(int ud, int s) { return done(0, 0, 0); }
int ibgts(int ud, int s) { return done(0, 0, 0); }
int ibpct(int ud) { return done(0, 0, 0); }
int ibsic(int ud) { return done(0, 0, 0); }
int ibsre(int ud, int v) { return done(0, 0, 0); }
int ibtrg(int ud) { return done(0, 0, 0); }
int ibwait(int ud, int m) { return done(0, 0, 0); }
