// Helper for the Python fuzz-parity test: reads {flags, env, contexts} on stdin,
// writes the TypeScript evaluator's results as JSON on stdout.
import { evaluateAll } from "../dist/evaluator.js";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const { flags, env, contexts } = JSON.parse(input);
  process.stdout.write(JSON.stringify(contexts.map((c) => evaluateAll(flags, env, c))));
});
