import { useState } from "react";
import PrereqScreen from "./PrereqScreen";
import "./App.css";

type WizardStep = "prereqs" | "signin" | "configure" | "deploy" | "summary";

function App() {
  const [step, setStep] = useState<WizardStep>("prereqs");

  return (
    <main className="container">
      {step === "prereqs" && (
        <PrereqScreen onAllSatisfied={() => setStep("signin")} />
      )}
      {step !== "prereqs" && (
        <div>
          <h1>Coming soon: {step}</h1>
          <p>This wizard step hasn't been implemented yet.</p>
        </div>
      )}
    </main>
  );
}

export default App;
