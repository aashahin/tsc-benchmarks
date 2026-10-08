# Third-party workload sources

## Microsoft compiler-union benchmark

- Repository: https://github.com/microsoft/typescript-benchmarking
- Commit: `41f652ab2df5077b1115f73eaeefc2fe9f674132`
- Directory: `cases/solutions/Compiler-Unions`
- Local source: `vendor/compiler-unions/`
- License: [Microsoft MIT license](vendor/compiler-unions/LICENSE)

The original source and configuration files are preserved byte-for-byte, including their original line endings. `SOURCE.json` records Git blob hashes, checked during preparation and tests. Unused upstream `built/local` output is omitted. Preparation copies the sources into the cache and adds a shared overlay with `strict: false` to preserve the older compiler workload's strictness defaults; it does not repair or suppress its TypeScript 7 diagnostics.

## Zod benchmark generator

- Repository: https://github.com/colinhacks/zod
- Commit: `0b216ef674e297ebe41d8bf902262e56f8755822`
- Upstream file: `packages/tsc/generate.ts`
- Local source: `vendor/zod/generate.ts`
- License: [Colin McDonnell MIT license](vendor/zod/LICENSE)

The generator is unmodified and hash-verified. The local driver seeds randomness, selects object and extend/omit chain workloads, and materializes their output before timing. Generated code imports the exact `zod@4.4.3` package; preparation records the installed package tree hash. This measures type-checking performance, not Zod runtime validation.

## Application setup recipes

Repository commits and compatibility preparation follow https://github.com/pingdotgg/ts-rust/tree/main/scripts/bench-apps. The runner and resource-control code are local implementations; the original scripts are not executed.

| Repository | Pinned commit | Configuration |
|---|---|---|
| microsoft/vscode | `3f07e1aba32acacb8b08ae91bfdc954b580ad1fd` | `src/tsconfig.json` |
| getsentry/sentry | `8294650589dbd26f230c73f4ab26b62a68aede8f` | `tsconfig.json` |
| microsoft/playwright | `d469960fdfc461e2d5795a3fa48a58a52a91ecaf` | `tsconfig.json` |
| excalidraw/excalidraw | `53973c3a423fbd75a4ce68107786b4fcb90e4968` | shared overlay of root `tsconfig.json` |
| typeorm/typeorm | `c64a1f052fc39f6688b6b73b83d065d7147ba8bb` | shared overlay of `packages/typeorm/tsconfig.json` |
| trpc/trpc | `d756e591a5e37ef20b8d75ecd4d736c195497289` | `packages/server/tsconfig.json` |

Each checkout retains its upstream license. Sentry is identified as source-available in the manifest; it is not represented as an OSI-licensed project. Checkouts and their dependency trees are cached locally and are not vendored into this project.
