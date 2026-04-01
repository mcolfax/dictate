cask "dictate" do
  version "1.4.3"
  sha256 :no_check  # Replace with output of: shasum -a 256 Dictate.dmg

  url "https://github.com/mcolfax/dictate/releases/download/v#{version}/Dictate.dmg"
  name "Dictate"
  desc "Free local AI dictation for macOS — no cloud, no subscription"
  homepage "https://github.com/mcolfax/dictate"

  depends_on macos: ">= :monterey"
  depends_on arch: :arm64

  app "Dictate.app"

  zap trash: [
    "~/.dictate",
    "~/Library/Logs/Dictate",
  ]
end
