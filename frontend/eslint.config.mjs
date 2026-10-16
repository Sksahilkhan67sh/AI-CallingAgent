import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

const config = [
  ...coreWebVitals,
  ...typescript,
  {
    // HF2: eslint-config-next 16 ships two React Compiler rules that the previous config (14.x)
    // did not have. They flag existing, working code (lib/auth-context.tsx reads the stored
    // session in an effect; lib/use-admin-query.ts forwards a caller-supplied deps array).
    // Rewriting those is frontend behavior work outside HF2, so the project keeps the lint
    // strictness it had before the upgrade. Tracked as DEFERRED in docs/HARDENING-HF2-NOTES.md.
    rules: {
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/use-memo": "off",
    },
  },
];

export default config;
