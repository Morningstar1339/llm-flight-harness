# Applies the sighting-system probe to David's Export.lua.
# Usage: python export_probe_patch.py <path-to-Export.lua>
# Additive and pcall-guarded; reversible by deleting the marked block.
import sys, io

path = sys.argv[1]
src = io.open(path, encoding='utf-8').read()

ANCHOR = """  local lock = try("LoGetLockedTargetInformation", LoGetLockedTargetInformation)"""
BLOCK = """  -- ==== sighting-system probe (added 7/20, autonomous-lock project) ====
  -- Purpose: learn whether the FC3 Su-27 exposes designator/TDC state.
  -- Fully pcall-guarded like everything else; delete this block to revert.
  local sight = try("LoGetSightingSystemInfo", LoGetSightingSystemInfo)
  if sight then
    p.sight = pick("sight.parse", function()
      -- shape is undocumented for FC3; ship it raw-ish and let the daemon look
      return sight
    end)
  end
  -- ==== end sighting-system probe ====

""" + ANCHOR

if 'sighting-system probe' in src:
    print("probe already present; nothing to do")
elif ANCHOR not in src:
    print("ANCHOR NOT FOUND — file differs from expected; NOT modifying. "
          "Paste Export.lua to Claude instead.")
else:
    io.open(path + '.bak', 'w', encoding='utf-8').write(src)
    io.open(path, 'w', encoding='utf-8').write(src.replace(ANCHOR, BLOCK))
    print(f"probe installed; backup at {path}.bak")
