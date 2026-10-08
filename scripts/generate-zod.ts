import { mkdirSync, renameSync } from "node:fs";
import { resolve } from "node:path";

const options = JSON.parse(process.argv[2]);
let seed = 20261008;
Math.random = () => {
  seed = Math.imul(seed, 1664525) + 1013904223 | 0;
  return (seed >>> 0) / 4294967296;
};
const generator = await import("../vendor/zod/generate.ts");
mkdirSync("src", { recursive: true });
if (options.generator === "objects") {
  generator.generate({ ...generator.ZOD, numSchemas: options.schemas, numKeys: options.keys,
    numRefs: 0, numOmits: 1, numPicks: 1, numExtends: 1 });
} else {
  for (let index = 0; index < options.chains; index++) {
    generator.generateExtendChain({ ...generator.ZOD, numSchemas: options.depth, numKeys: options.keys });
    renameSync(resolve("src/index.ts"), resolve(`src/chain-${index}.ts`));
  }
}
