import { Landing } from "@/components/landing/Landing";
import { LocaleAutoPick } from "@/components/landing/LocaleAutoPick";
import { messagesFor } from "@/lib/i18n";

/** `/`: the English landing page, which moves to the owner's own language if it can. */
export default function Home() {
  return (
    <>
      <Landing locale="en" t={messagesFor("en")} />
      <LocaleAutoPick />
    </>
  );
}
