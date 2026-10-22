import IconBase, { type IconProps } from './IconBase';

/** Attach a file to a chat message. */
export default function PaperclipIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9" />
    </IconBase>
  );
}
