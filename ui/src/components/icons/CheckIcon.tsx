import IconBase, { type IconProps } from './IconBase';

/** A check mark, e.g. the selected row of a radio list. */
export default function CheckIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </IconBase>
  );
}
