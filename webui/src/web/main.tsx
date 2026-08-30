import { render } from "solid-js/web";
import { App } from "./App.tsx";
import { start } from "./store.ts";
import "./index.css";

const root = document.getElementById("root");
if (!root) throw new Error("no #root to render into");

start();
render(() => <App />, root);
