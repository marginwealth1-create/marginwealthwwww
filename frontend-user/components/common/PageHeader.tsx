import { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { BackButton } from "@/components/common/BackButton";

interface Props {
  title: string;
  description?: string;
  actions?: ReactNode;
  className?: string;
  /** Show a round back-arrow to the left of the title. */
  back?: boolean;
  /** Where the back-arrow goes; omit to use browser-history back. */
  backHref?: string;
}

export function PageHeader({ title, description, actions, className, back, backHref }: Props) {
  return (
    <header className={cn("flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div className="flex items-center gap-2.5">
        {back && <BackButton href={backHref} />}
        <div>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
          {description && <p className="text-sm text-muted-foreground">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </header>
  );
}
