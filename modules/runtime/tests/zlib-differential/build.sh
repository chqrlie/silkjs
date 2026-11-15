#!/bin/sh
# Builds the differential harness: the runtime's zlib adapter (src/ext/zlib_stream.silk over
# src/stdlib_copy) as an object with C exports, zlib 1.3.1 from its pinned release, and the
# programs that drive both with the same calls. Needs the Silk toolchain, gcc and curl.
#
#   tests/zlib-differential/build.sh            # writes build/zlib-differential/
#   build/zlib-differential/fuzz_deflate 1 20000    # seeds 1 to 20000; a third argument traces
#   build/zlib-differential/fuzz_inflate 1 20000
#   build/zlib-differential/bench_codec <a large file> 5
set -e
here=$(cd "$(dirname "$0")" && pwd)
runtime=$(cd "$here/../.." && pwd)
out=$runtime/build/zlib-differential
mkdir -p "$out/zlib" "$out/package/src/ext"
cd "$out"
if [ ! -f zlib/deflate.c ]; then
  curl -fsSLo zlib.tgz https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz
  echo "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23  zlib.tgz" | sha256sum -c -
  tar xzf zlib.tgz -C zlib --strip-components 1
fi
# The flags the runtime's zlib package had.
for name in adler32 crc32 deflate inflate inftrees inffast trees zutil; do
  gcc -O2 -w -c -DZ_HAVE_UNISTD_H -D_LARGEFILE64_SOURCE=1 -Izlib "zlib/$name.c" -o "$name.o"
done
gcc -O2 -w -c -Izlib "$here/zref.c" -o zref.o
rm -f libzref.a && ar rcs libzref.a zref.o adler32.o crc32.o deflate.o inflate.o inftrees.o inffast.o trees.o zutil.o
# A package of its own, so that the build reaches only the adapter and the codecs.
cp "$here/silk.toml" package/silk.toml
cp "$here/src/zshim.silk" package/src/zshim.silk
ln -sfn "$runtime/src/stdlib_copy" package/src/stdlib_copy
ln -sfn "$runtime/src/ext/zlib_stream.silk" package/src/ext/zlib_stream.silk
rm -f zshim.o
(cd package && silk build src/zshim.silk --emit object -o "$out/zshim.o")
objcopy --redefine-sym main=zs_unused_main zshim.o zshim_lib.o
for name in fuzz_deflate fuzz_inflate bench_codec; do
  gcc -O2 -w -no-pie -o "$name" "$here/$name.c" zshim_lib.o libzref.a
done
echo "built $out"
